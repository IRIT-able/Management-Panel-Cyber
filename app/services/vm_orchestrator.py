"""VM orchestration service for Proxmox with per-node template VMID mappings."""

from flask import current_app
from .proxmox_client import ProxmoxClient
from ..extensions import db
from ..models import (
    VirtualMachine,
    Student,
    Classroom,
    VMTemplate,
    TemplateNodeMapping,
    NodeConfiguration,
    NodeStorageConfig
)
import time
import random


# -------------------------------------------------------------
# PROXMOX CLIENT BUILDER
# -------------------------------------------------------------
def get_proxmox_client() -> ProxmoxClient:
    return ProxmoxClient(
        host=current_app.config["PROXMOX_HOST"],
        user=current_app.config.get("PROXMOX_USER"),
        token_name=current_app.config.get("PROXMOX_TOKEN_NAME"),
        token_value=current_app.config.get("PROXMOX_TOKEN_VALUE"),
        password=current_app.config.get("PROXMOX_PASSWORD"),
        ssh_host=current_app.config.get("PROXMOX_SSH_HOST"),
        ssh_user=current_app.config.get("PROXMOX_SSH_USER", "root"),
        ssh_key_path=current_app.config.get("PROXMOX_SSH_KEY_PATH"),
    )


# -------------------------------------------------------------
# NODE INITIALIZATION
# -------------------------------------------------------------
def initialize_nodes():
    """Sync DB node list with actual Proxmox nodes."""
    prox = get_proxmox_client()
    nodes = prox.get_nodes()

    for node_name in nodes:
        exists = NodeConfiguration.query.filter_by(node_name=node_name).first()
        if not exists:
            cfg = NodeConfiguration(
                node_name=node_name,
                max_vms=current_app.config.get("MAX_VMS_PER_NODE", 12),
                storage_pool=current_app.config.get("DEFAULT_VM_STORAGE", "local-lvm"),
            )
            db.session.add(cfg)

    db.session.commit()


# -------------------------------------------------------------
# NODE SELECTION
# -------------------------------------------------------------
def select_best_node(strategy: str = None, template: VMTemplate = None):
    """
    Select the best node for deployment.
    If template is provided, only consider nodes where the template is registered.
    """
    if strategy is None:
        strategy = current_app.config.get("NODE_SELECTION_STRATEGY", "least_vms")

    nodes = [
        n for n in NodeConfiguration.query.filter_by(is_active=True).all()
        if n.is_available_for_deployment()
    ]
    
    # Filter to only nodes that have the template registered
    if template:
        available_node_names = template.get_available_nodes()
        nodes = [n for n in nodes if n.node_name in available_node_names]
    
    if not nodes:
        return None

    if strategy == "least_vms":
        return min(nodes, key=lambda n: n.get_current_vm_count())
    if strategy == "random":
        return random.choice(nodes)
    if strategy == "priority":
        return max(nodes, key=lambda n: n.priority)

    return min(nodes, key=lambda n: n.get_current_vm_count())


# -------------------------------------------------------------
# TEMPLATE VALIDATION
# -------------------------------------------------------------
def ensure_template_on_node(template: VMTemplate, node_name: str) -> int:
    """
    Get the template VMID for a given node.
    Raises RuntimeError if template is not registered on the node.
    """
    try:
        return template.get_template_id_for_node(node_name)
    except RuntimeError as e:
        raise RuntimeError(
            f"Template '{template.name}' is NOT registered on node '{node_name}'.\n"
            f"Available nodes: {', '.join(template.get_available_nodes())}\n"
            f"➡ You must specify the template VMID for this node when creating the template."
        )


# -------------------------------------------------------------
# STORAGE SELECTION
# -------------------------------------------------------------
def _count_vms_on_storage(node_name: str, storage_name: str) -> int:
    q = VirtualMachine.query.filter_by(proxmox_node=node_name)
    if storage_name:
        q = q.filter_by(storage=storage_name)
    return q.count()


def _choose_storage_for_node(node_cfg: NodeConfiguration):
    storages = node_cfg.storages.filter_by(active=True).all()
    if not storages:
        return node_cfg.get_next_storage()

    weighted = []
    for sc in storages:
        used = _count_vms_on_storage(node_cfg.node_name, sc.name)
        if sc.max_vms is not None and used >= sc.max_vms:
            continue
        weighted.extend([sc.name] * max(1, sc.weight))

    if not weighted:
        return None

    idx = node_cfg.storage_rr_index % len(weighted)
    choice = weighted[idx]
    node_cfg.storage_rr_index += 1
    db.session.commit()

    return choice


# -------------------------------------------------------------
# MAIN VM DEPLOYMENT LOGIC
# -------------------------------------------------------------
def deploy_vm_for_student(student_id: int, template_id: int, node: str = None):
    prox = get_proxmox_client()

    student = Student.query.get(student_id)
    if not student:
        raise ValueError(f"Student {student_id} not found")

    template = VMTemplate.query.get(template_id)
    if not template or not template.is_active:
        raise ValueError("Template not found or inactive")

    if node is None:
        node_cfg = select_best_node(template=template)
        if not node_cfg:
            available = template.get_available_nodes()
            raise RuntimeError(
                f"No available nodes for template '{template.name}'. "
                f"Template is registered on: {', '.join(available)}"
            )
        node = node_cfg.node_name
    else:
        node_cfg = NodeConfiguration.query.filter_by(node_name=node).first()
        if not node_cfg:
            raise RuntimeError(f"Node '{node}' not found")

    # Validate template exists on this node
    proxmox_template_id = ensure_template_on_node(template, node)

    # Get VMID
    new_vmid = prox.get_next_vmid()

    # Clean name formatting
    clean_class = ''.join(c if c.isalnum() else '-' for c in student.classroom.name.lower())
    clean_student = ''.join(c if c.isalnum() else '-' for c in student.name.lower())
    vm_name = f"{clean_class}-{clean_student}-{new_vmid}"

    use_linked = current_app.config.get("USE_LINKED_CLONES", True)

    # For linked clones, storage is inherited from template - don't specify it
    # For full clones, we need to specify storage
    if use_linked:
        storage = None
    else:
        storage = _choose_storage_for_node(node_cfg)
        if not storage:
            storage = current_app.config.get("DEFAULT_VM_STORAGE", "local-lvm")

    prox.clone_vm(
        node=node,
        template_id=proxmox_template_id,
        new_vmid=new_vmid,
        name=vm_name,
        storage=storage,
        linked=use_linked
    )

    prox.optimize_vm_for_performance(node, new_vmid)
    prox.start_vm(node, new_vmid)

    time.sleep(1)
    cfg = prox.get_vm_config(node, new_vmid)
    ip = None
    if "ipconfig0" in cfg:
        try:
            ip = cfg["ipconfig0"].split("=")[1].split("/")[0]
        except Exception:
            pass

    console_url = prox.get_console_url(node, new_vmid)

    vm = VirtualMachine(
        student_id=student_id,
        proxmox_vmid=new_vmid,
        proxmox_node=node,
        template_name=template.name,
        console_url=console_url,
        status="running",
        storage=storage,
        ip_address=ip
    )
    db.session.add(vm)
    db.session.commit()

    return vm


def get_vm_status(node: str, vmid: int) -> dict:
    """Get the current status of a VM"""
    prox = get_proxmox_client()
    try:
        status = prox.get_vm_status(node, vmid)
        return status
    except Exception as e:
        error_msg = str(e).lower()
        if "does not exist" in error_msg or "not found" in error_msg or "no such file" in error_msg:
            return {'status': 'deleted', 'error': str(e)}
        else:
            return {'status': 'unknown', 'error': str(e)}


def stop_vm_for_student(student_id: int) -> VirtualMachine:
    """Stop the VM for a student"""
    vm = VirtualMachine.query.filter_by(student_id=student_id).first()
    if not vm:
        raise ValueError(f"No VM found for student {student_id}")
    
    prox = get_proxmox_client()
    try:
        prox.stop_vm(vm.proxmox_node, vm.proxmox_vmid)
        vm.status = "stopped"
        db.session.commit()
        return vm
    except Exception as e:
        raise RuntimeError(f"Failed to stop VM: {str(e)}")


def start_vm_for_student(student_id: int) -> VirtualMachine:
    """Start the VM for a student"""
    vm = VirtualMachine.query.filter_by(student_id=student_id).first()
    if not vm:
        raise ValueError(f"No VM found for student {student_id}")
    
    prox = get_proxmox_client()
    try:
        prox.start_vm(vm.proxmox_node, vm.proxmox_vmid)
        vm.status = "running"
        db.session.commit()
        return vm
    except Exception as e:
        raise RuntimeError(f"Failed to start VM: {str(e)}")


def deploy_vms_for_students(student_ids: list, template_id: int) -> list:
    """Deploy VMs for multiple students at once"""
    deployed_vms = []
    for student_id in student_ids:
        try:
            vm = deploy_vm_for_student(student_id, template_id)
            deployed_vms.append(vm)
        except Exception as e:
            # Log but continue with other students
            print(f"Failed to deploy VM for student {student_id}: {str(e)}")
            continue
    
    return deployed_vms


def delete_vm_for_student(vm_id: int):
    """Delete a VM both from Proxmox and the database"""
    vm = VirtualMachine.query.get(vm_id)
    if not vm:
        raise ValueError(f"VM {vm_id} not found")
    
    prox = get_proxmox_client()
    
    try:
        # Delete from Proxmox
        prox.delete_vm(vm.proxmox_node, vm.proxmox_vmid)
    except Exception as e:
        from flask import current_app
        if current_app:
            current_app.logger.warning(f"Failed to delete VM {vm_id} from Proxmox: {str(e)}")
    
    # Delete from database
    try:
        db.session.delete(vm)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        raise RuntimeError(f"Failed to delete VM from database: {str(e)}")
    

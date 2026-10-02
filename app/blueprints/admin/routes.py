from flask import render_template, redirect, url_for, flash, request, send_file, abort, Response, jsonify
from flask_login import login_required
from . import bp
from .forms import CreateTeacherForm, CreateVMTemplateForm, NodeConfigurationForm, MultiNodeSettingsForm
from ...models import User, Classroom, Student, VirtualMachine, VMTemplate, NodeConfiguration, VMTemplateReplica
from ...extensions import db
from ...security import admin_required, hash_password
import os


@bp.route('/')
@login_required
@admin_required
def dashboard():
    """IT Admin dashboard"""
    from ...services.vm_orchestrator import initialize_nodes
    
    # Initialize nodes if needed
    try:
        initialize_nodes()
    except Exception as e:
        flash(f'Warning: Could not initialize nodes: {str(e)}', 'warning')
    
    teachers = User.query.filter_by(role='teacher').all()
    templates = VMTemplate.query.filter_by(is_active=True).all()
    nodes = NodeConfiguration.query.all()
    
    # Statistics
    total_classes = Classroom.query.count()
    total_students = Student.query.count()
    total_vms = VirtualMachine.query.count()
    
    # Node statistics
    node_stats = []
    for node in nodes:
        vm_count = node.get_current_vm_count()
        node_stats.append({
            'name': node.node_name,
            'vm_count': vm_count,
            'max_vms': node.max_vms,
            'utilization': (vm_count / node.max_vms * 100) if node.max_vms > 0 else 0,
            'is_active': node.is_active,
            'available': node.is_available_for_deployment()
        })
    
    stats = {
        'teachers': len(teachers),
        'classes': total_classes,
        'students': total_students,
        'vms': total_vms,
        'nodes': len(nodes),
        'active_nodes': len([n for n in nodes if n.is_active])
    }
    
    return render_template('admin/dashboard.html', 
                         teachers=teachers, 
                         templates=templates,
                         nodes=node_stats,
                         stats=stats)

@bp.route('/teacher/<int:teacher_id>/reset_password', methods=['POST'])
@login_required
@admin_required
def reset_teacher_password(teacher_id):
    """Reset a teacher's password"""
    teacher = User.query.get_or_404(teacher_id)
    
    if teacher.role != 'teacher':
        flash('Can only reset passwords for teachers', 'danger')
        return redirect(url_for('admin.dashboard'))
        
    new_password = request.form.get('new_password')
    if not new_password or len(new_password) < 6:
        flash('Password must be at least 6 characters', 'danger')
        return redirect(url_for('admin.dashboard'))
        
    teacher.password_hash = hash_password(new_password)
    # Unlock account if it was locked
    teacher.failed_login_attempts = 0
    teacher.locked_until = None
    
    db.session.commit()
    flash(f'Password reset successfully for {teacher.email}', 'success')
    return redirect(url_for('admin.dashboard'))


@bp.route('/logs', methods=['GET'])
@login_required
@admin_required
def logs_page():
    """Admin page to download auth logs"""
    return render_template('admin/logs.html')


@bp.route('/logs/download', methods=['GET'])
@login_required
@admin_required
def download_logs():
    """Download the last 10,000 lines of the application system logs (journalctl)"""
    import subprocess
    from flask import Response
    
    try:
        # Fetch the last 10,000 lines of the systemd journal for the app service
        result = subprocess.run(
            ["journalctl", "-u", "cyberlab-admin", "-n", "10000", "--no-pager"],
            capture_output=True,
            text=False
        )
        
        if result.returncode != 0 or not result.stdout:
            data = b"No logs available or permission denied reading journalctl."
        else:
            data = result.stdout
            
    except Exception as e:
        data = f"Error fetching logs: {e}".encode('utf-8')
        
    return Response(
        data,
        mimetype="text/plain",
        headers={"Content-disposition": "attachment; filename=cyberlab-system.log"}
    )


@bp.route('/teachers/create', methods=['GET', 'POST'])
@login_required
@admin_required
def create_teacher():
    """Create a new teacher account"""
    form = CreateTeacherForm()
    
    if form.validate_on_submit():
        # Check if username already exists
        existing = User.query.filter_by(email=form.username.data).first()
        if existing:
            flash('A user with this username already exists', 'danger')
            return render_template('admin/create_teacher.html', form=form)
        
        try:
            # Create teacher user
            teacher = User(
                email=form.username.data,
                password_hash=hash_password(form.password.data),
                role='teacher'
            )
            
            # Auto credential / noVNC only; no external remote user provisioning
            
            db.session.add(teacher)
            db.session.commit()
            
            flash(f'Teacher account created successfully! Username: {teacher.email}', 'success')
            return redirect(url_for('admin.dashboard'))
        
        except Exception as e:
            db.session.rollback()
            flash(f'Error creating teacher: {str(e)}', 'danger')
    
    return render_template('admin/create_teacher.html', form=form)


@bp.route('/teachers/<int:teacher_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_teacher(teacher_id):
    """Delete a teacher account"""
    teacher = User.query.get_or_404(teacher_id)
    
    if teacher.role != 'teacher':
        flash('Cannot delete non-teacher users', 'danger')
        return redirect(url_for('admin.dashboard'))
    
    try:
        # Delete teacher and all cascading data
        db.session.delete(teacher)
        db.session.commit()
        flash(f'Teacher {teacher.email} deleted successfully', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting teacher: {str(e)}', 'danger')
    
    return redirect(url_for('admin.dashboard'))


@bp.route('/templates/create', methods=['GET', 'POST'])
@login_required
@admin_required
def create_template():
    """Register a new VM template with per-node VMID mappings"""
    from ...models import TemplateNodeMapping
    
    form = CreateVMTemplateForm()
    nodes = NodeConfiguration.query.filter_by(is_active=True).all()
    
    if form.validate_on_submit():
        # Create the template
        template = VMTemplate(
            name=form.name.data,
            description=form.description.data,
            memory=form.memory.data,
            cores=form.cores.data,
            is_active=form.is_active.data
        )
        
        try:
            db.session.add(template)
            db.session.flush()  # Get template.id without committing
            
            # Process node-VMID mappings from request form
            node_vmid_map = {}
            for node in nodes:
                vmid_field = f'node_{node.id}_vmid'
                if vmid_field in request.form and request.form.get(vmid_field):
                    try:
                        vmid = int(request.form.get(vmid_field))
                        node_vmid_map[node.node_name] = vmid
                    except (ValueError, TypeError):
                        pass
            
            if not node_vmid_map:
                db.session.rollback()
                flash('Error: You must specify at least one node with a template VMID', 'danger')
                return render_template('admin/create_template.html', form=form, nodes=nodes)
            
            # Create mappings for each node
            for node_name, vmid in node_vmid_map.items():
                mapping = TemplateNodeMapping(
                    template_id=template.id,
                    proxmox_node=node_name,
                    proxmox_template_id=vmid
                )
                db.session.add(mapping)
            
            db.session.commit()
            flash(f'VM template "{template.name}" created successfully with {len(node_vmid_map)} node mapping(s)', 'success')
            return redirect(url_for('admin.dashboard'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error creating template: {str(e)}', 'danger')
    
    return render_template('admin/create_template.html', form=form, nodes=nodes)


@bp.route('/templates/<int:template_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_template(template_id):
    """Delete a VM template"""
    template = VMTemplate.query.get_or_404(template_id)
    
    try:
        db.session.delete(template)
        db.session.commit()
        flash(f'Template "{template.name}" deleted successfully', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting template: {str(e)}', 'danger')
    
    return redirect(url_for('admin.dashboard'))


@bp.route('/nodes')
@login_required
@admin_required
def nodes():
    """Node management page"""
    nodes = NodeConfiguration.query.all()
    # Build per-storage counts for active storages
    from ...models import VirtualMachine, NodeStorageConfig
    storage_counts = {}
    for node in nodes:
        sc_list = []
        # Prefer structured storage configs
        try:
            stor_cfgs = node.storages.filter_by(active=True).all()
        except Exception:
            stor_cfgs = []
        if stor_cfgs:
            for sc in stor_cfgs:
                count = VirtualMachine.query.filter_by(proxmox_node=node.node_name, storage=sc.name).count()
                sc_list.append({
                    'name': sc.name,
                    'count': count,
                    'max_vms': sc.max_vms
                })
        else:
            # Fallback to legacy CSV list
            for name in (node.get_storages_list() or []):
                count = VirtualMachine.query.filter_by(proxmox_node=node.node_name, storage=name).count()
                sc_list.append({
                    'name': name,
                    'count': count,
                    'max_vms': None
                })
        storage_counts[node.id] = sc_list
    return render_template('admin/nodes.html', nodes=nodes, storage_counts=storage_counts)


@bp.route('/api/nodes/<string:node_name>/storages', methods=['GET'])
@login_required
@admin_required
def api_node_storages(node_name: str):
    """Return storages for a given Proxmox node"""
    try:
        from ...services.vm_orchestrator import get_proxmox_client
        proxmox = get_proxmox_client()
        storages = proxmox.get_node_storages(node_name)
        # Normalize to simple list with id and status/enabled
        resp = []
        for s in storages or []:
            # Proxmox returns fields like 'storage', 'type', 'active'
            name = s.get('storage') or s.get('id') or s.get('name')
            if name:
                resp.append({
                    'name': name,
                    'type': s.get('type'),
                    'active': s.get('active', 1) in (1, True, '1')
                })
        return jsonify({'ok': True, 'node': node_name, 'storages': resp})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@bp.route('/nodes/create', methods=['GET', 'POST'])
@login_required
@admin_required
def create_node():
    """Create or configure a node"""
    form = NodeConfigurationForm()
    
    if form.validate_on_submit():
        # Check if node already exists
        existing = NodeConfiguration.query.filter_by(node_name=form.node_name.data).first()
        if existing:
            flash(f'Node "{form.node_name.data}" already exists', 'danger')
        else:
            # Optional: verify node exists in Proxmox cluster
            try:
                from ...services.vm_orchestrator import get_proxmox_client
                proxmox = get_proxmox_client()
                cluster_nodes = proxmox.get_nodes()
                if form.node_name.data not in cluster_nodes:
                    flash(f'Warning: Node "{form.node_name.data}" not found in Proxmox cluster. It will be saved but unused until it exists.', 'warning')
            except Exception:
                # If Proxmox check fails, continue silently
                pass

            node = NodeConfiguration(
                node_name=form.node_name.data,
                max_vms=form.max_vms.data,
                storage_pools=form.storage_pools.data,
                priority=form.priority.data,
                is_active=form.is_active.data
            )
            
            try:
                db.session.add(node)
                db.session.commit()
                # Create storage configs if provided via picker
                selected = request.form.get('selected_storages', '').strip()
                if selected:
                    names = [x.strip() for x in selected.split(',') if x.strip()]
                    for name in names:
                        from ...models import NodeStorageConfig
                        sc = NodeStorageConfig(node_id=node.id, name=name, weight=1, max_vms=None, active=True)
                        db.session.add(sc)
                    # also mirror into CSV for convenience
                    node.storage_pools = ', '.join(names)
                    db.session.commit()
                flash(f'Node "{node.node_name}" configured successfully', 'success')
                return redirect(url_for('admin.nodes'))
            except Exception as e:
                db.session.rollback()
                flash(f'Error configuring node: {str(e)}', 'danger')
    
    return render_template('admin/create_node.html', form=form)


@bp.route('/nodes/<int:node_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_node(node_id):
    """Edit node configuration"""
    node = NodeConfiguration.query.get_or_404(node_id)
    form = NodeConfigurationForm(obj=node)
    if node.storage_pools:
        form.storage_pools.data = node.storage_pools
    else:
        form.storage_pools.data = node.storage_pool

    if form.validate_on_submit():
        node.node_name = form.node_name.data
        node.max_vms = form.max_vms.data
        node.storage_pools = form.storage_pools.data
        node.priority = form.priority.data
        node.is_active = form.is_active.data

        try:
            from ...models import NodeStorageConfig

            # 1) Create / upsert storages from the picker
            selected = request.form.get('selected_storages', '').strip()
            if selected:
                selected_names = [x.strip() for x in selected.split(',') if x.strip()]
                for name in selected_names:
                    sc = NodeStorageConfig.query.filter_by(node_id=node.id, name=name).first()
                    if not sc:
                        sc = NodeStorageConfig(
                            node_id=node.id,
                            name=name,
                            weight=1,
                            max_vms=None,
                            active=True
                        )
                        db.session.add(sc)
                # Make sure node.storage_pools mirrors everything
                existing_pools = [x.strip() for x in (node.storage_pools or '').split(',') if x.strip()]
                merged = sorted(set(existing_pools + selected_names))
                node.storage_pools = ', '.join(merged)

            # 2) Update weights / max_vms / active flags for *all* storages in the table
            names = request.form.getlist('storage_name')
            weights = request.form.getlist('storage_weight')
            maxvms = request.form.getlist('storage_max_vms')

            form_map = request.form.to_dict(flat=False)
            active_flags = form_map.get('storage_active', [])

            for i, n in enumerate(names):
                sc = NodeStorageConfig.query.filter_by(node_id=node.id, name=n).first()
                if not sc:
                    sc = NodeStorageConfig(node_id=node.id, name=n)
                    db.session.add(sc)

                try:
                    sc.weight = int(weights[i]) if weights and i < len(weights) and weights[i].strip() != '' else 1
                except Exception:
                    sc.weight = 1

                try:
                    mv = maxvms[i].strip() if maxvms and i < len(maxvms) else ''
                    sc.max_vms = int(mv) if mv != '' else None
                except Exception:
                    sc.max_vms = None

                # Any checkbox present in order = active; missing index = inactive
                sc.active = i < len(active_flags)

            db.session.commit()
            flash(f'Node "{node.node_name}" updated successfully', 'success')
            return redirect(url_for('admin.nodes'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating node: {str(e)}', 'danger')

    # GET path: just render the form
    return render_template('admin/edit_node.html', form=form, node=node)

    """Edit node configuration"""
    node = NodeConfiguration.query.get_or_404(node_id)

    # Map model to form, preferring storage_pools
    form = NodeConfigurationForm(obj=node)
    if node.storage_pools:
        form.storage_pools.data = node.storage_pools
    else:
        form.storage_pools.data = node.storage_pool

    if form.validate_on_submit():
        node.node_name = form.node_name.data
        node.max_vms = form.max_vms.data
        node.storage_pools = form.storage_pools.data
        node.priority = form.priority.data
        node.is_active = form.is_active.data

        try:
            from ...models import NodeStorageConfig

            # ---- 1) Parse storage_pools CSV into a list of names ----
            csv_names = [
                x.strip()
                for x in (node.storage_pools or "").split(",")
                if x.strip()
            ]

            # ---- 2) Load all existing storage configs for this node ----
            existing_cfgs = NodeStorageConfig.query.filter_by(node_id=node.id).all()
            existing_by_name = {sc.name: sc for sc in existing_cfgs}

            # First mark everything inactive by default
            for sc in existing_cfgs:
                sc.active = False

            # ---- 3) Upsert configs for everything in storage_pools ----
            for name in csv_names:
                sc = existing_by_name.get(name)
                if not sc:
                    # New storage: create with default weight/max_vms
                    sc = NodeStorageConfig(
                        node_id=node.id,
                        name=name,
                        weight=1,
                        max_vms=None,
                        active=True,
                    )
                    db.session.add(sc)
                else:
                    # Existing storage: mark as active again
                    sc.active = True

            # Optional: keep weights / max_vms from the table if present
            # (ONLY if you want to preserve current behavior)
            # names = request.form.getlist("storage_name")
            # weights = request.form.getlist("storage_weight")
            # maxvms = request.form.getlist("storage_max_vms")
            # for i, n in enumerate(names):
            #     n = (n or "").strip()
            #     if not n:
            #         continue
            #     sc = existing_by_name.get(n)
            #     if not sc:
            #         continue
            #     try:
            #         sc.weight = (
            #             int(weights[i])
            #             if weights and i < len(weights) and weights[i].strip() != ""
            #             else 1
            #         )
            #     except Exception:
            #         sc.weight = 1
            #     try:
            #         mv = maxvms[i].strip() if maxvms and i < len(maxvms) else ""
            #         sc.max_vms = int(mv) if mv != "" else None
            #     except Exception:
            #         sc.max_vms = None

            db.session.commit()
            flash(f'Node "{node.node_name}" updated successfully', "success")
            return redirect(url_for("admin.nodes"))

        except Exception as e:
            db.session.rollback()
            flash(f"Error updating node: {str(e)}", "danger")

    # GET or failed validation: just render the template
    return render_template("admin/edit_node.html", form=form, node=node)

    """Edit node configuration"""
    node = NodeConfiguration.query.get_or_404(node_id)

    # Map model to form, preferring storage_pools
    form = NodeConfigurationForm(obj=node)
    if node.storage_pools:
        form.storage_pools.data = node.storage_pools
    else:
        form.storage_pools.data = node.storage_pool

    if form.validate_on_submit():
        node.node_name = form.node_name.data
        node.max_vms = form.max_vms.data
        node.storage_pools = form.storage_pools.data
        node.priority = form.priority.data
        node.is_active = form.is_active.data

        try:
            from ...models import NodeStorageConfig

            # ---- 1) Update / create storages from the table rows ----
            names = request.form.getlist("storage_name")
            weights = request.form.getlist("storage_weight")
            maxvms = request.form.getlist("storage_max_vms")
            form_map = request.form.to_dict(flat=False)
            active_flags = form_map.get("storage_active", [])

            seen_names = []

            for i, n in enumerate(names):
                n = (n or "").strip()
                if not n:
                    continue

                seen_names.append(n)

                sc = NodeStorageConfig.query.filter_by(
                    node_id=node.id, name=n
                ).first()
                if not sc:
                    sc = NodeStorageConfig(node_id=node.id, name=n)
                    db.session.add(sc)

                # weight
                try:
                    sc.weight = (
                        int(weights[i])
                        if weights and i < len(weights) and weights[i].strip() != ""
                        else 1
                    )
                except Exception:
                    sc.weight = 1

                # max_vms
                try:
                    mv = maxvms[i].strip() if maxvms and i < len(maxvms) else ""
                    sc.max_vms = int(mv) if mv != "" else None
                except Exception:
                    sc.max_vms = None

                # Active flag: checkbox presence by index
                sc.active = i < len(active_flags)

            # ---- 2) Any storages not in the form rows get marked inactive ----
            all_cfgs = NodeStorageConfig.query.filter_by(node_id=node.id).all()
            for sc in all_cfgs:
                if sc.name not in seen_names:
                    sc.active = False

            db.session.commit()
            flash(f'Node "{node.node_name}" updated successfully', "success")
            return redirect(url_for("admin.nodes"))

        except Exception as e:
            db.session.rollback()
            flash(f"Error updating node: {str(e)}", "danger")

    # GET or failed validation: just render the template
    return render_template("admin/edit_node.html", form=form, node=node)




@bp.route('/settings/multi-node', methods=['GET', 'POST'])
@login_required
@admin_required
def multi_node_settings():
    """Multi-node system settings"""
    form = MultiNodeSettingsForm()
    
    # Load current settings from config/environment
    if request.method == 'GET':
        from flask import current_app
        form.max_vms_per_node.data = current_app.config.get('MAX_VMS_PER_NODE', 12)
        form.use_linked_clones.data = current_app.config.get('USE_LINKED_CLONES', True)
        form.node_selection_strategy.data = current_app.config.get('NODE_SELECTION_STRATEGY', 'least_vms')
    
    if form.validate_on_submit():
        # Update all existing node configurations
        try:
            NodeConfiguration.query.update({
                NodeConfiguration.max_vms: form.max_vms_per_node.data
            })
            db.session.commit()
            
            flash('Multi-node settings updated successfully. Note: Some settings require application restart.', 'success')
            flash('Consider updating your .env file with the new settings for persistence.', 'info')
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating settings: {str(e)}', 'danger')
    
    return render_template('admin/multi_node_settings.html', form=form)

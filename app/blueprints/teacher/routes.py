from flask import render_template, redirect, url_for, flash, request, jsonify
import time
from flask_login import login_required, current_user
from . import bp
from .forms import ImportClassForm, AddStudentForm, DeployVMForm
from ...models import User, Classroom, Student, VirtualMachine, VMTemplate, NodeConfiguration, TemplateNodeMapping, Assignment, Submission
from ...extensions import db
from ...security import teacher_required
from flask import send_from_directory, current_app
import os
from ...services.vm_orchestrator import (
    deploy_vm_for_student,
    get_proxmox_client,
    delete_vm_for_student
)
from ...extensions import scheduler
from flask import current_app
import re
import secrets
import csv
from io import StringIO
import subprocess
import os


def parse_ansible_inventory():
    """Parse the ansible inventory hosts file and return list of student PCs.
    Returns list of dicts: [{'name': 'PC1', 'ip': '192.168.1.24'}, ...]
    """
    inventory_path = "/home/admin/Admin-Panel/cyberlab-admin/ansible/inventory/hosts"
    pcs = []
    try:
        with open(inventory_path, 'r') as f:
            in_students = False
            for line in f:
                line = line.strip()
                if line == '[students]':
                    in_students = True
                    continue
                if line.startswith('[') and line != '[students]':
                    in_students = False
                    continue
                if in_students and line and not line.startswith('#'):
                    # Parse lines like: PC1  ansible_host=192.168.1.24
                    parts = line.split()
                    if len(parts) >= 2:
                        name = parts[0]
                        ip = None
                        for part in parts[1:]:
                            if part.startswith('ansible_host='):
                                ip = part.split('=', 1)[1]
                        pcs.append({'name': name, 'ip': ip or 'unknown'})
                    elif len(parts) == 1:
                        pcs.append({'name': parts[0], 'ip': parts[0]})
    except Exception:
        pass
    return pcs


def generate_student_credentials(name: str):
    """Generate a unique username and random password from a student name"""
    base = re.sub(r'[^a-zA-Z0-9]+', '-', name.strip().lower())
    base = re.sub(r'-{2,}', '-', base).strip('-')
    if not base:
        base = 'student'
    candidate = base
    counter = 1
    from ...models import Student
    while Student.query.filter_by(username=candidate).first():
        counter += 1
        candidate = f"{base}{counter}"
    password = secrets.token_urlsafe(8)  # ~11 chars, URL safe
    return candidate, password


@bp.route('/')
@login_required
@teacher_required
def dashboard():
    """Teacher dashboard showing all classes"""
    if current_user.is_admin():
        classrooms = Classroom.query.all()
    else:
        classrooms = current_user.classrooms.all()
    
    # Get student PCs from ansible inventory for freeze dropdown
    student_pcs = parse_ansible_inventory()
    
    return render_template('teacher/dashboard.html', classrooms=classrooms, student_pcs=student_pcs)


@bp.route('/import', methods=['GET', 'POST'])
@login_required
@teacher_required
def import_class():
    """Import a new class with students"""
    form = ImportClassForm()
    
    if form.validate_on_submit():
        try:
            # Create classroom
            classroom = Classroom(
                name=form.class_name.data,
                teacher_id=current_user.id
            )
            db.session.add(classroom)
            db.session.flush()  # Get classroom ID

            # Parse student names (one per line)
            student_names = [line.strip() for line in form.students_text.data.split('\n') if line.strip()]

            # Create students with generated credentials
            generated_summary = []
            for name in student_names:
                username, plain_pw = generate_student_credentials(name)
                student = Student(name=name, classroom_id=classroom.id, username=username, is_active=True)
                student.set_password(plain_pw)
                # Store encrypted initial password so teachers can view it; don't store plaintext.
                try:
                    student.set_initial_password(plain_pw)
                except Exception:
                    # If encryption isn't configured, fall back to not storing initial password.
                    pass
                # Do NOT store plaintext passwords. Provide credentials as a one-time download below.
                db.session.add(student)
                db.session.flush()

                generated_summary.append((name, username, plain_pw))

            db.session.commit()
            # Redirect to class details; passwords will be rendered inline per student
            flash(f'Class "{classroom.name}" imported with {len(student_names)} students!', 'success')
            return redirect(url_for('teacher.class_detail', class_id=classroom.id))
        except Exception as e:
            db.session.rollback()
            flash(f'Error importing class: {str(e)}', 'danger')

    return render_template('teacher/import_class.html', form=form)


@bp.route('/class/<int:class_id>')
@login_required
@teacher_required
def class_detail(class_id):
    """View class details with students and VMs"""
    classroom = Classroom.query.get_or_404(class_id)
    
    # Check permission
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))
    
    students = classroom.students.all()
    templates = VMTemplate.query.filter_by(is_active=True).all()
    
    # Prepare student data with VMs
    student_data = []
    for student in students:
        vms = student.vms.all()
        # Decrypt initial password for display to teachers (requires FERNET_KEY configured)
        try:
            initial_pw = student.get_initial_password()
        except Exception:
            initial_pw = None
        student_data.append({
            'student': student,
            'vms': vms,
            'initial_password': initial_pw
        })
    
    # Get student PCs from ansible inventory for freeze dropdown
    student_pcs = parse_ansible_inventory()
    
    return render_template('teacher/class_detail.html', 
                         classroom=classroom,
                         student_data=student_data,
                         templates=templates,
                         student_pcs=student_pcs)


@bp.route('/class/<int:class_id>/assignment', methods=['POST'])
@login_required
@teacher_required
def create_assignment(class_id):
    """Create a new assignment for the class"""
    classroom = Classroom.query.get_or_404(class_id)
    
    # Check permission
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))
        
    name = request.form.get('assignment_name')
    if not name:
        flash('Assignment name is required', 'danger')
        return redirect(url_for('teacher.class_detail', class_id=class_id))
        
    assignment = Assignment(name=name, classroom_id=class_id)
    db.session.add(assignment)
    db.session.commit()
    
    flash(f'Assignment "{name}" created successfully', 'success')
    return redirect(url_for('teacher.class_detail', class_id=class_id))


@bp.route('/class/<int:class_id>/assignment/<int:assignment_id>')
@login_required
@teacher_required
def assignment_detail(class_id, assignment_id):
    """View submissions for a specific assignment"""
    classroom = Classroom.query.get_or_404(class_id)
    
    # Check permission
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))
        
    assignment = Assignment.query.get_or_404(assignment_id)
    if assignment.classroom_id != class_id:
        return redirect(url_for('teacher.class_detail', class_id=class_id))
        
    students = classroom.students.all()
    submissions_map = {sub.student_id: sub for sub in assignment.submissions.all()}
    
    return render_template('teacher/assignment_detail.html', 
                         classroom=classroom,
                         assignment=assignment,
                         students=students,
                         submissions_map=submissions_map)


@bp.route('/submission/<int:submission_id>/grade', methods=['POST'])
@login_required
@teacher_required
def grade_submission(submission_id):
    """Grade a submission"""
    submission = Submission.query.get_or_404(submission_id)
    classroom = submission.student.classroom
    
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))
        
    submission.grade = request.form.get('grade')
    submission.feedback = request.form.get('feedback')
    db.session.commit()
    
    flash('Grade updated successfully', 'success')
    return redirect(url_for('teacher.assignment_detail', class_id=classroom.id, assignment_id=submission.assignment_id))


@bp.route('/submission/image/<int:submission_id>')
@login_required
@teacher_required
def view_submission_image(submission_id):
    """Serve the submission screenshot"""
    submission = Submission.query.get_or_404(submission_id)
    classroom = submission.student.classroom
    
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        return "Access denied", 403
        
    filename = os.path.basename(submission.image_path)
    directory = os.path.join(current_app.root_path, 'uploads', 'submissions')
    return send_from_directory(directory, filename)


@bp.route('/assignment/<int:assignment_id>/toggle', methods=['POST'])
@login_required
@teacher_required
def toggle_assignment_visibility(assignment_id):
    """Toggle visibility of an assignment for students"""
    assignment = Assignment.query.get_or_404(assignment_id)
    classroom = assignment.classroom
    
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))
        
    assignment.is_visible = not assignment.is_visible
    db.session.commit()
    
    status = "visible to" if assignment.is_visible else "hidden from"
    flash(f'Assignment "{assignment.name}" is now {status} students.', 'success')
    return redirect(url_for('teacher.class_detail', class_id=classroom.id))


@bp.route('/class/<int:class_id>/credentials.csv', methods=['GET'])
@login_required
@teacher_required
def download_class_credentials(class_id):
    """Download CSV of all students' credentials for a class"""
    classroom = Classroom.query.get_or_404(class_id)
    # Permission check
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['name', 'username', 'password'])
    for student in classroom.students.order_by(Student.name.asc()).all():
        try:
            pw = student.get_initial_password()
        except Exception:
            pw = None
        writer.writerow([student.name or '', student.username or '', pw or ''])

    csv_data = output.getvalue()
    from flask import make_response
    resp = make_response(csv_data)
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = f'attachment; filename=class_{classroom.id}_credentials.csv'
    return resp


@bp.route('/class/<int:class_id>/bulk_vm_cleanup', methods=['POST'])
@login_required
@teacher_required
def class_bulk_vm_cleanup(class_id):
    """Stop and delete all VMs for every student in the class"""
    classroom = Classroom.query.get_or_404(class_id)

    # Permission check
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))

    stopped = 0
    deleted = 0
    errors = []

    proxmox = get_proxmox_client()

    # Iterate through students and their VMs
    for student in classroom.students.all():
        for vm in student.vms.all():
            try:
                # Always attempt stop first (idempotent)
                try:
                    if vm.status == 'running':
                        stop_vm_for_student(vm.id)
                        stopped += 1
                except Exception as e:
                    errors.append(f'Stop VM {vm.proxmox_vmid}: {e}')

                # Poll Proxmox status until not running (max ~30s)
                for attempt in range(15):
                    try:
                        status_data = proxmox.get_vm_status(vm.proxmox_node, vm.proxmox_vmid)
                        current_status = status_data.get('status') or status_data.get('qmpstatus')
                    except Exception as poll_err:
                        current_status = vm.status  # fallback
                        errors.append(f'Status poll VM {vm.proxmox_vmid}: {poll_err}')
                    if current_status and current_status.lower() != 'running':
                        break
                    time.sleep(2)

                # Delete with wait complete
                try:
                    delete_vm_for_student(vm.id)
                    deleted += 1
                except Exception as e:
                    errors.append(f'Delete VM {vm.proxmox_vmid}: {e}')
            except Exception as e:
                errors.append(f'VM {vm.proxmox_vmid} general error: {e}')

    if deleted:
        flash(f'Bulk VM cleanup complete: stopped {stopped}, deleted {deleted} VMs.', 'success')
    else:
        flash('No VMs found to clean up.', 'info')

    if errors:
        flash('Some errors occurred: ' + '; '.join(errors[:5]) + (' ...' if len(errors) > 5 else ''), 'warning')

    return redirect(url_for('teacher.class_detail', class_id=class_id))


@bp.route('/class/<int:class_id>/add_student', methods=['POST'])
@login_required
@teacher_required
def add_student(class_id):
    """Add a single student to a class"""
    classroom = Classroom.query.get_or_404(class_id)
    
    # Check permission
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))
    
    student_name = request.form.get('student_name', '').strip()
    
    if not student_name:
        flash('Student name is required', 'danger')
        return redirect(url_for('teacher.class_detail', class_id=class_id))
    
    # Generate credentials
    username, plain_pw = generate_student_credentials(student_name)
    
    try:
        student = Student(name=student_name, classroom_id=classroom.id, username=username, is_active=True)
        student.set_password(plain_pw)
        try:
            student.set_initial_password(plain_pw)
        except Exception:
            pass
        # Do NOT store plaintext passwords. Provide credentials as a one-time download below.
        db.session.add(student)
        db.session.commit()
        # Show credentials inline on the page for teacher convenience
        flash(f'Added student {student_name} (username: {username}) — initial password shown in the list below.', 'success')
        return redirect(url_for('teacher.class_detail', class_id=class_id))
    except Exception as e:
        db.session.rollback()
        flash(f'Error adding student: {str(e)}', 'danger')
    
    return redirect(url_for('teacher.class_detail', class_id=class_id))


@bp.route('/console/<int:vm_id>')
@login_required
@teacher_required
def console(vm_id):
    """Embedded console view for a VM"""
    vm = VirtualMachine.query.get_or_404(vm_id)
    classroom = vm.student.classroom
    
    # Check permission
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))
    
    # Pass VM info to template - WebSocket will be proxied through Flask
    return render_template('teacher/console.html', 
                         vm=vm,
                         node=vm.proxmox_node,
                         vmid=vm.proxmox_vmid)


@bp.route('/student/<int:student_id>/delete', methods=['POST'])
@login_required
@teacher_required
def delete_student(student_id):
    """Delete a student"""
    student = Student.query.get_or_404(student_id)
    classroom = student.classroom
    
    # Check permission
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))
    
    try:
        # Delete all VMs for this student
        for vm in student.vms.all():
            try:
                delete_vm_for_student(vm.id)
            except Exception as e:
                flash(f'Warning: Could not delete VM {vm.proxmox_vmid}: {str(e)}', 'warning')
        
        db.session.delete(student)
        db.session.commit()
        flash(f'Student "{student.name}" deleted', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting student: {str(e)}', 'danger')
    
    return redirect(url_for('teacher.class_detail', class_id=classroom.id))


@bp.route('/student/<int:student_id>/force_delete', methods=['POST'])
@login_required
@teacher_required
def force_delete_student(student_id):
    """Force delete a student from the database without touching VMs.
    Use when VMs have been deleted externally and normal delete fails."""
    student = Student.query.get_or_404(student_id)
    classroom = student.classroom
    class_id = classroom.id

    # Check permission
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))

    student_name = student.name
    try:
        # Delete VM records from DB only (no Proxmox calls)
        for vm in student.vms.all():
            db.session.delete(vm)
        db.session.delete(student)
        db.session.commit()
        flash(f'Force deleted student "{student_name}" and removed VM records from database.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error force deleting student: {str(e)}', 'danger')

    return redirect(url_for('teacher.class_detail', class_id=class_id))


@bp.route('/student/<int:student_id>/deploy_vm', methods=['POST'])
@login_required
@teacher_required
def deploy_vm(student_id):
    """Deploy a VM for a student"""
    student = Student.query.get_or_404(student_id)
    classroom = student.classroom
    
    # Check permission
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))
    
    template_id = request.form.get('template_id', type=int)
    if not template_id:
        flash('Template ID is required', 'danger')
        return redirect(url_for('teacher.class_detail', class_id=classroom.id))
    
    template = VMTemplate.query.get(template_id)
    if not template:
        flash('Invalid template', 'danger')
        return redirect(url_for('teacher.class_detail', class_id=classroom.id))
    
    try:
        vm = deploy_vm_for_student(
            student_id=student_id,
            template_id=template.id  # Use database template ID, not Proxmox template ID
        )
        flash(f'VM deployed successfully for {student.name}! VM ID: {vm.proxmox_vmid} on node {vm.proxmox_node}', 'success')
    except Exception as e:
        flash(f'Error deploying VM: {str(e)}', 'danger')
    
    return redirect(url_for('teacher.class_detail', class_id=classroom.id))


@bp.route('/vm/<int:vm_id>/stop', methods=['POST'])
@login_required
@teacher_required
def stop_vm_route(vm_id):
    """Stop a VM"""
    from ...services.vm_orchestrator import stop_vm_for_student
    
    vm = VirtualMachine.query.get_or_404(vm_id)
    student = vm.student
    classroom = student.classroom
    
    # Check permission
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))
    
    try:
        stop_vm_for_student(student.id)
        flash(f'VM {vm.proxmox_vmid} stopped successfully', 'success')
    except Exception as e:
        flash(f'Error stopping VM: {str(e)}', 'danger')
    
    return redirect(url_for('teacher.class_detail', class_id=classroom.id))


@bp.route('/vm/<int:vm_id>/start', methods=['POST'])
@login_required
@teacher_required
def start_vm_route(vm_id):
    """Start a VM"""
    from ...services.vm_orchestrator import start_vm_for_student
    
    vm = VirtualMachine.query.get_or_404(vm_id)
    student = vm.student
    classroom = student.classroom
    
    # Check permission
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))
    
    try:
        start_vm_for_student(student.id)
        flash(f'VM {vm.proxmox_vmid} started successfully', 'success')
    except Exception as e:
        flash(f'Error starting VM: {str(e)}', 'danger')
    
    return redirect(url_for('teacher.class_detail', class_id=classroom.id))


@bp.route('/vm/<int:vm_id>/delete', methods=['POST'])
@login_required
@teacher_required
def delete_vm_route(vm_id):
    """Delete a VM"""
    vm = VirtualMachine.query.get_or_404(vm_id)
    student = vm.student
    classroom = student.classroom
    
    # Check permission
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))
    
    try:
        delete_vm_for_student(vm_id)
        flash(f'VM {vm.proxmox_vmid} deleted successfully', 'success')
    except Exception as e:
        flash(f'Error deleting VM: {str(e)}', 'danger')
    
    return redirect(url_for('teacher.class_detail', class_id=classroom.id))


@bp.route('/student/<int:student_id>/reset_password', methods=['POST'])
@login_required
@teacher_required
def reset_student_password(student_id):
    """Reset a student's password and update the stored initial password."""
    student = Student.query.get_or_404(student_id)
    classroom = student.classroom

    # Permission check
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))

    # Generate a new secure password
    new_pw = secrets.token_urlsafe(10)

    try:
        student.set_password(new_pw)
        try:
            student.set_initial_password(new_pw)
        except Exception:
            pass
        db.session.commit()
        flash(f'Reset password for {student.name} (username: {student.username}). New password shown below.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error resetting password: {str(e)}', 'danger')

    return redirect(url_for('teacher.class_detail', class_id=classroom.id))


@bp.route('/class/<int:class_id>/delete', methods=['POST'])
@login_required
@teacher_required
def delete_class(class_id):
    """Delete a class"""
    classroom = Classroom.query.get_or_404(class_id)
    
    # Check permission
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))
    
    try:
        # Cascading delete will handle students and VMs
        db.session.delete(classroom)
        db.session.commit()
        flash(f'Class "{classroom.name}" deleted successfully', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting class: {str(e)}', 'danger')
    
    return redirect(url_for('teacher.dashboard'))


@bp.route('/class/<int:class_id>/deploy_bulk_vms', methods=['POST'])
@login_required
@teacher_required
def deploy_bulk_vms(class_id):
    """Deploy VMs for multiple students at once"""
    classroom = Classroom.query.get_or_404(class_id)
    
    # Check permission
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))
    
    template_id = request.form.get('template_id', type=int)
    selected_student_ids = request.form.getlist('student_ids', type=int)
    
    if not template_id:
        flash('Template ID is required', 'danger')
        return redirect(url_for('teacher.class_detail', class_id=class_id))
    
    if not selected_student_ids:
        flash('Please select at least one student', 'danger')
        return redirect(url_for('teacher.class_detail', class_id=class_id))
    
    template = VMTemplate.query.get(template_id)
    if not template or not template.is_active:
        flash('Invalid or inactive template', 'danger')
        return redirect(url_for('teacher.class_detail', class_id=class_id))
    
    # Verify all students belong to this classroom
    students = Student.query.filter(Student.id.in_(selected_student_ids)).all()
    if not all(student.classroom_id == class_id for student in students):
        flash('Invalid student selection', 'danger')
        return redirect(url_for('teacher.class_detail', class_id=class_id))
    
    try:
        from ...tasks import async_deploy_vms
        import threading
        threading.Thread(
            target=async_deploy_vms,
            args=[selected_student_ids, template_id, current_app._get_current_object().app_context()]
        ).start()
        flash(f'Deployment of VMs started in the background for {len(selected_student_ids)} students.', 'success')
        
    except Exception as e:
        flash(f'Error starting bulk deployment: {str(e)}', 'danger')
    
    return redirect(url_for('teacher.class_detail', class_id=class_id))

@bp.route('/class/<int:class_id>/bulk_start', methods=['POST'])
@login_required
@teacher_required
def bulk_start_vms(class_id):
    """Start all VMs for a class"""
    classroom = Classroom.query.get_or_404(class_id)
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))
        
    from ...tasks import async_start_all_vms
    import threading
    threading.Thread(
        target=async_start_all_vms,
        args=[class_id, current_app._get_current_object().app_context()]
    ).start()
    flash('Starting all VMs in the background.', 'success')
    return redirect(url_for('teacher.class_detail', class_id=class_id))

@bp.route('/class/<int:class_id>/bulk_stop', methods=['POST'])
@login_required
@teacher_required
def bulk_stop_vms(class_id):
    """Stop all VMs for a class"""
    classroom = Classroom.query.get_or_404(class_id)
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))
        
    from ...tasks import async_stop_all_vms
    import threading
    threading.Thread(
        target=async_stop_all_vms,
        args=[class_id, current_app._get_current_object().app_context()]
    ).start()
    flash('Stopping all VMs in the background.', 'success')
    return redirect(url_for('teacher.class_detail', class_id=class_id))


@bp.route('/class/<int:class_id>/open_cyber_portal', methods=['POST'])
@login_required
@teacher_required
def open_cyber_portal(class_id):
    """Run ansible playbook to open cybersecurity portal on student machines"""
    classroom = Classroom.query.get_or_404(class_id)

    # Check permission
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))

    import subprocess
    import os
    from flask import current_app

    try:
        # Your ansible directory is fixed here
        ansible_dir = "/home/admin/Admin-Panel/cyberlab-admin/ansible"
        playbook_path = os.path.join(ansible_dir, "inventory", "open_cyberlab_site.yml")
        inventory_path = os.path.join(ansible_dir, "inventory", "hosts")

        # Validate files exist
        if not os.path.exists(playbook_path):
            flash(f'Ansible playbook not found at {playbook_path}', 'danger')
            return redirect(url_for('teacher.class_detail', class_id=class_id))

        if not os.path.exists(inventory_path):
            flash(f'Ansible inventory not found at {inventory_path}', 'danger')
            return redirect(url_for('teacher.class_detail', class_id=class_id))

        # Fix environment for ansible when running under Flask (systemd/gunicorn PATH is often minimal)
        env = os.environ.copy()
        env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        env["ANSIBLE_SSH_EXECUTABLE"] = "/usr/bin/ssh"
        env.setdefault("HOME", "/home/admin")

        # Run ansible-playbook with the correct inventory
        result = r = subprocess.run(
            ["/usr/bin/ansible-playbook", "-i", inventory_path, playbook_path],
            cwd=ansible_dir,
            capture_output=True,
            text=True,
            timeout=120,
            env=env
        )

        if result.returncode == 0:
            flash('Successfully opened cybersecurity portal on student machines!', 'success')
        else:
            error_msg = result.stderr or result.stdout or "Unknown error"
            flash(f'Ansible playbook failed: {error_msg[:400]}', 'danger')

            # Optional: log full output server-side for debugging
            current_app.logger.error("Ansible failed (rc=%s)\nSTDOUT:\n%s\nSTDERR:\n%s",
                                     result.returncode, result.stdout, result.stderr)

    except subprocess.TimeoutExpired:
        flash('Ansible playbook timed out after 120 seconds', 'warning')
    except Exception as e:
        flash(f'Error running ansible playbook: {str(e)}', 'danger')

    return redirect(url_for('teacher.class_detail', class_id=class_id))
@bp.route('/class/<int:class_id>/toggle_freeze', methods=['POST'])
@login_required
@teacher_required
def toggle_freeze(class_id):
    """Run ansible playbook to open cybersecurity portal on student machines"""
    classroom = Classroom.query.get_or_404(class_id)

    # Check permission
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))

    import subprocess
    import os
    from flask import current_app

    try:
        # Your ansible directory is fixed here
        ansible_dir = "/home/admin/Admin-Panel/cyberlab-admin/ansible"
        playbook_path = os.path.join(ansible_dir, "inventory", "freeze_students_input.yml")
        inventory_path = os.path.join(ansible_dir, "inventory", "hosts")

        # Validate files exist
        if not os.path.exists(playbook_path):
            flash(f'Ansible playbook not found at {playbook_path}', 'danger')
            return redirect(url_for('teacher.class_detail', class_id=class_id))

        if not os.path.exists(inventory_path):
            flash(f'Ansible inventory not found at {inventory_path}', 'danger')
            return redirect(url_for('teacher.class_detail', class_id=class_id))

        # Fix environment for ansible when running under Flask (systemd/gunicorn PATH is often minimal)
        env = os.environ.copy()
        env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        env["ANSIBLE_SSH_EXECUTABLE"] = "/usr/bin/ssh"
        env.setdefault("HOME", "/home/admin")

        # Run ansible-playbook with the correct inventory
        result = r = subprocess.run(
            ["/usr/bin/ansible-playbook", "-i", inventory_path, playbook_path],
            cwd=ansible_dir,
            capture_output=True,
            text=True,
            timeout=120,
            env=env
        )

        if result.returncode == 0:
            flash('Successfully Toggled Freeze on Student PCs', 'success')
        else:
            error_msg = result.stderr or result.stdout or "Unknown error"
            flash(f'Ansible playbook failed: {error_msg[:400]}', 'danger')

            # Optional: log full output server-side for debugging
            current_app.logger.error("Ansible failed (rc=%s)\nSTDOUT:\n%s\nSTDERR:\n%s",
                                     result.returncode, result.stdout, result.stderr)

    except subprocess.TimeoutExpired:
        flash('Ansible playbook timed out after 120 seconds', 'warning')
    except Exception as e:
        flash(f'Error running ansible playbook: {str(e)}', 'danger')

    return redirect(url_for('teacher.class_detail', class_id=class_id))


@bp.route('/api/student_pcs')
@login_required
@teacher_required
def api_student_pcs():
    """API endpoint to get list of student PCs from ansible inventory"""
    pcs = parse_ansible_inventory()
    return jsonify(pcs)


@bp.route('/freeze_pc', methods=['POST'])
@login_required
@teacher_required
def freeze_individual_pc():
    """Toggle freeze on an individual student PC by hostname using existing playbook with --limit"""
    pc_name = request.form.get('pc_name', '').strip()

    if not pc_name:
        flash('No PC selected', 'danger')
        return redirect(url_for('teacher.dashboard'))

    # Validate pc_name is in the inventory (security: prevent arbitrary host targeting)
    valid_pcs = parse_ansible_inventory()
    valid_names = [pc['name'] for pc in valid_pcs]
    if pc_name not in valid_names:
        flash(f'PC "{pc_name}" not found in inventory', 'danger')
        return redirect(url_for('teacher.dashboard'))

    try:
        ansible_dir = "/home/admin/Admin-Panel/cyberlab-admin/ansible"
        playbook_path = os.path.join(ansible_dir, "inventory", "freeze_students_input.yml")
        inventory_path = os.path.join(ansible_dir, "inventory", "hosts")

        # Validate files exist
        if not os.path.exists(playbook_path):
            flash(f'Ansible playbook not found at {playbook_path}', 'danger')
            return redirect(url_for('teacher.dashboard'))

        if not os.path.exists(inventory_path):
            flash(f'Ansible inventory not found at {inventory_path}', 'danger')
            return redirect(url_for('teacher.dashboard'))

        # Fix environment for ansible
        env = os.environ.copy()
        env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        env["ANSIBLE_SSH_EXECUTABLE"] = "/usr/bin/ssh"
        env.setdefault("HOME", "/home/admin")

        # Run the existing freeze playbook with --limit to target specific PC
        result = r = subprocess.run(
            [
                "/usr/bin/ansible-playbook",
                "-i", inventory_path,
                playbook_path,
                "--limit", pc_name,
            ],
            cwd=ansible_dir,
            capture_output=True,
            text=True,
            timeout=60,
            env=env
        )

        if result.returncode == 0:
            flash(f'Toggled freeze on {pc_name} successfully!', 'success')
        else:
            error_msg = result.stderr or result.stdout or "Unknown error"
            flash(f'Failed to toggle freeze on {pc_name}: {error_msg[:300]}', 'danger')
            current_app.logger.error("Ansible individual freeze failed (rc=%s)\nSTDOUT:\n%s\nSTDERR:\n%s",
                                     result.returncode, result.stdout, result.stderr)

    except subprocess.TimeoutExpired:
        flash(f'Freeze command for {pc_name} timed out after 60 seconds', 'warning')
    except Exception as e:
        flash(f'Error freezing {pc_name}: {str(e)}', 'danger')

    return redirect(request.referrer or url_for('teacher.dashboard'))


@bp.route('/toggle_freeze_all', methods=['POST'])
@login_required
@teacher_required
def toggle_freeze_all():
    """Toggle freeze on all student PCs using existing playbook (no --limit)"""
    try:
        ansible_dir = "/home/admin/Admin-Panel/cyberlab-admin/ansible"
        playbook_path = os.path.join(ansible_dir, "inventory", "freeze_students_input.yml")
        inventory_path = os.path.join(ansible_dir, "inventory", "hosts")

        if not os.path.exists(playbook_path):
            flash(f'Ansible playbook not found at {playbook_path}', 'danger')
            return redirect(request.referrer or url_for('teacher.dashboard'))

        if not os.path.exists(inventory_path):
            flash(f'Ansible inventory not found at {inventory_path}', 'danger')
            return redirect(request.referrer or url_for('teacher.dashboard'))

        env = os.environ.copy()
        env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        env["ANSIBLE_SSH_EXECUTABLE"] = "/usr/bin/ssh"
        env.setdefault("HOME", "/home/admin")

        result = r = subprocess.run(
            ["/usr/bin/ansible-playbook", "-i", inventory_path, playbook_path],
            cwd=ansible_dir,
            capture_output=True,
            text=True,
            timeout=120,
            env=env
        )

        if result.returncode == 0:
            flash('Successfully toggled freeze on all student PCs!', 'success')
        else:
            error_msg = result.stderr or result.stdout or "Unknown error"
            flash(f'Ansible playbook failed: {error_msg[:400]}', 'danger')
            current_app.logger.error("Ansible failed (rc=%s)\nSTDOUT:\n%s\nSTDERR:\n%s",
                                     result.returncode, result.stdout, result.stderr)

    except subprocess.TimeoutExpired:
        flash('Ansible playbook timed out after 120 seconds', 'warning')
    except Exception as e:
        flash(f'Error running ansible playbook: {str(e)}', 'danger')

    return redirect(request.referrer or url_for('teacher.dashboard'))

@bp.route('/pc_status')
@login_required
@teacher_required
def pc_status():
    """Live status of all student PCs"""
    import tempfile
    import json
    import shutil
    import re
    
    # Run ansible ad-hoc command to check freeze state
    temp_dir = tempfile.mkdtemp()
    ansible_dir = "/home/admin/Admin-Panel/cyberlab-admin/ansible"
    inventory_path = os.path.join(ansible_dir, "inventory", "hosts")
    
    env = os.environ.copy()
    env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    env["ANSIBLE_SSH_EXECUTABLE"] = "/usr/bin/ssh"
    env["ANSIBLE_TIMEOUT"] = "3"
    env["ANSIBLE_HOST_KEY_CHECKING"] = "False"
    env.setdefault("HOME", "/home/admin")
    
    r = subprocess.run(
        [
            "/usr/bin/ansible", "students",
            "-i", inventory_path,
            "-m", "stat",
            "-a", "path=/var/lib/cyberlab/input_frozen",
            "-f", "30",
            "--tree", temp_dir
        ],
        cwd=ansible_dir,
        capture_output=True,
        env=env
    )
    
    pcs = []
    valid_pcs = parse_ansible_inventory()
    
    for pc in valid_pcs:
        pc_name = pc['name']
        ip = pc['ip']
        status = {
            'name': pc_name,
            'ip': ip,
            'reachable': False,
            'frozen': False,
            'error': None
        }
        
        result_file = os.path.join(temp_dir, pc_name)
        if os.path.exists(result_file):
            try:
                with open(result_file, 'r') as f:
                    data = json.load(f)
                    
                if data.get('unreachable'):
                    status['reachable'] = False
                    status['error'] = data.get('msg', 'Unreachable')
                elif data.get('failed'):
                    status['reachable'] = False
                    status['error'] = data.get('msg', 'Failed')
                else:
                    status['reachable'] = True
                    stat_info = data.get('stat', {})
                    if stat_info and stat_info.get('exists'):
                        status['frozen'] = True
            except Exception as e:
                status['error'] = str(e)
        else:
            status['error'] = "No response or offline"
            
        pcs.append(status)
        
    shutil.rmtree(temp_dir, ignore_errors=True)
    
    def sort_key(pc):
        match = re.search(r'\d+', pc['name'])
        if match:
            return int(match.group())
        return 0
        
    pcs.sort(key=sort_key)
    
    return render_template('teacher/pc_status.html', pcs=pcs)

@bp.route('/pc/<pc_name>/screenshot')
@login_required
@teacher_required
def pc_screenshot(pc_name):
    import subprocess
    import tempfile
    import os
    from flask import send_file
    
    # Get IP
    valid_pcs = parse_ansible_inventory()
    ip = None
    for pc in valid_pcs:
        if pc['name'] == pc_name:
            ip = pc['ip']
            break
            
    if not ip:
        return "PC Not found", 404
        
    temp_img = tempfile.mktemp(suffix=".jpg")
    try:
        r = subprocess.run(
            ["/usr/local/bin/vncsnapshot", "-allowblank", f"{ip}:0", temp_img],
            timeout=2,
            capture_output=True
        )
        if not os.path.exists(temp_img):
            err_msg = f"VNC_ERR_MARKER: {r.stderr.decode('utf-8') if r.stderr else ''} {r.stdout.decode('utf-8') if r.stdout else ''}"
            import sys
            print(err_msg, file=sys.stderr, flush=True)
            return err_msg, 500
        return send_file(temp_img, mimetype='image/jpeg', max_age=1)
    except subprocess.TimeoutExpired:
        return "Timeout", 504
    except Exception as e:
        return str(e), 500

@bp.route('/pc/<pc_name>/console')
@login_required
@teacher_required
def pc_console(pc_name):
    valid_pcs = parse_ansible_inventory()
    ip = None
    for pc in valid_pcs:
        if pc['name'] == pc_name:
            ip = pc['ip']
            break
    if not ip:
        return "PC not found", 404
        
    return render_template('teacher/pc_console.html', pc_name=pc_name, ip=ip)


@bp.route('/freeze_all', methods=['POST'])
def freeze_all_pcs():
    try:
        ansible_dir = "/home/admin/Admin-Panel/cyberlab-admin/ansible"
        playbook_path = os.path.join(ansible_dir, "inventory", "freeze_students_input.yml")
        inventory_path = os.path.join(ansible_dir, "inventory", "hosts")

        env = os.environ.copy()
        env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        env["ANSIBLE_SSH_EXECUTABLE"] = "/usr/bin/ssh"
        env.setdefault("HOME", "/home/admin")

        result = subprocess.run(
            ["ansible-playbook", "-i", inventory_path, playbook_path, "-e", '{"force_action": "freeze"}'],
            cwd=ansible_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=60
        )
        if result.returncode == 0:
            flash('Successfully froze all student PCs!', 'success')
        else:
            flash(f'Failed to freeze all PCs. Check logs.', 'danger')
    except Exception as e:
        flash(f'Error freezing all PCs: {str(e)}', 'danger')
    return redirect(url_for('teacher.pc_status'))

@bp.route('/unfreeze_all', methods=['POST'])
def unfreeze_all_pcs():
    try:
        ansible_dir = "/home/admin/Admin-Panel/cyberlab-admin/ansible"
        playbook_path = os.path.join(ansible_dir, "inventory", "freeze_students_input.yml")
        inventory_path = os.path.join(ansible_dir, "inventory", "hosts")

        env = os.environ.copy()
        env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        env["ANSIBLE_SSH_EXECUTABLE"] = "/usr/bin/ssh"
        env.setdefault("HOME", "/home/admin")

        result = subprocess.run(
            ["ansible-playbook", "-i", inventory_path, playbook_path, "-e", '{"force_action": "unfreeze"}'],
            cwd=ansible_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=60
        )
        if result.returncode == 0:
            flash('Successfully unfroze all student PCs!', 'success')
        else:
            flash(f'Failed to unfreeze all PCs. Check logs.', 'danger')
    except Exception as e:
        flash(f'Error unfreezing all PCs: {str(e)}', 'danger')
    return redirect(url_for('teacher.pc_status'))


@bp.route('/class/<int:class_id>/open_packet_tracer', methods=['POST'])
def open_packet_tracer(class_id):
    classroom = Classroom.query.get_or_404(class_id)
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))

    import subprocess
    import os
    try:
        ansible_dir = "/home/admin/Admin-Panel/cyberlab-admin/ansible"
        playbook_path = os.path.join(ansible_dir, "inventory", "open_packet_tracer.yml")
        inventory_path = os.path.join(ansible_dir, "inventory", "hosts")

        if not os.path.exists(playbook_path):
            flash('Packet Tracer playbook not found, but action registered!', 'warning')
            return redirect(url_for('teacher.class_detail', class_id=class_id))

        env = os.environ.copy()
        env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        env["ANSIBLE_SSH_EXECUTABLE"] = "/usr/bin/ssh"
        env.setdefault("HOME", "/home/admin")

        result = subprocess.run(
            ["ansible-playbook", "-i", inventory_path, playbook_path],
            cwd=ansible_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=60
        )
        if result.returncode == 0:
            flash('Successfully opened Packet Tracer on all student PCs!', 'success')
        else:
            flash(f'Failed to open Packet Tracer.', 'danger')
    except Exception as e:
        flash(f'Error opening Packet Tracer: {str(e)}', 'danger')
    return redirect(url_for('teacher.class_detail', class_id=class_id))


@bp.route('/class/<int:class_id>/close_all_windows', methods=['POST'])
def close_all_windows(class_id):
    classroom = Classroom.query.get_or_404(class_id)
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))

    import subprocess
    import os
    try:
        ansible_dir = "/home/admin/Admin-Panel/cyberlab-admin/ansible"
        playbook_path = os.path.join(ansible_dir, "inventory", "close_all_windows.yml")
        inventory_path = os.path.join(ansible_dir, "inventory", "hosts")

        if not os.path.exists(playbook_path):
            flash('Close All Windows playbook not found, but action registered!', 'warning')
            return redirect(url_for('teacher.class_detail', class_id=class_id))

        env = os.environ.copy()
        env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        env["ANSIBLE_SSH_EXECUTABLE"] = "/usr/bin/ssh"
        env.setdefault("HOME", "/home/admin")

        result = subprocess.run(
            ["ansible-playbook", "-i", inventory_path, playbook_path],
            cwd=ansible_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=60
        )
        if result.returncode == 0:
            flash('Successfully closed windows on all student PCs!', 'success')
        else:
            flash(f'Failed to close windows.', 'danger')
    except Exception as e:
        flash(f'Error closing windows: {str(e)}', 'danger')
    return redirect(url_for('teacher.class_detail', class_id=class_id))


@bp.route('/assignment/<int:assignment_id>/delete', methods=['POST'])
@login_required
@teacher_required
def delete_assignment(assignment_id):
    assignment = Assignment.query.get_or_404(assignment_id)
    classroom = assignment.classroom
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))
    
    db.session.delete(assignment)
    db.session.commit()
    flash('Assignment deleted successfully.', 'success')
    return redirect(url_for('teacher.class_detail', class_id=classroom.id))

@bp.route('/class/<int:class_id>/push_file', methods=['POST'])
@login_required
@teacher_required
def push_file_to_students(class_id):
    """Upload a file and push it to all student PCs via Ansible"""
    from werkzeug.utils import secure_filename
    import subprocess
    import shutil
    
    classroom = Classroom.query.get_or_404(class_id)
    if not current_user.is_admin() and classroom.teacher_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('teacher.dashboard'))
        
    if 'assignment_file' not in request.files:
        flash('No file part', 'danger')
        return redirect(url_for('teacher.class_detail', class_id=class_id))
        
    file = request.files['assignment_file']
    if file.filename == '':
        flash('No selected file', 'danger')
        return redirect(url_for('teacher.class_detail', class_id=class_id))
        
    if file:
        filename = secure_filename(file.filename)
        # Save temporarily
        temp_dir = os.path.join(current_app.root_path, 'uploads', 'temp_push')
        os.makedirs(temp_dir, exist_ok=True)
        file_path = os.path.join(temp_dir, filename)
        file.save(file_path)
        
        # Run ansible playbook
        ansible_dir = os.path.join(os.path.dirname(os.path.dirname(current_app.root_path)), "ansible")
        inventory_path = os.path.join(ansible_dir, "inventory", "hosts.yml")
        playbook_path = os.path.join(ansible_dir, "push_file_to_students.yml")
        
        try:
            env = os.environ.copy()
            env.setdefault("HOME", "/home/admin")
            extra_vars = f'{{"src_file": "{file_path}", "dest_filename": "{filename}"}}'
            result = subprocess.run(
                ["/usr/bin/ansible-playbook", "-i", inventory_path, playbook_path, "-e", extra_vars],
                cwd=ansible_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if result.returncode == 0:
                flash(f'Successfully pushed {filename} to all student computers!', 'success')
            else:
                current_app.logger.error(f"Failed to push file: {result.stderr}")
                flash(f'Failed to push file. Some computers may be offline.', 'warning')
                
        except Exception as e:
            current_app.logger.error(f"Error pushing file: {e}")
            flash('An error occurred while pushing the file.', 'danger')
            
        finally:
            # Clean up temp file
            if os.path.exists(file_path):
                os.remove(file_path)
                
        return redirect(url_for('teacher.class_detail', class_id=class_id))

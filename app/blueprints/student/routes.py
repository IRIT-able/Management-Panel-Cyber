from flask import render_template, redirect, url_for, flash, session, request, jsonify
from functools import wraps
from . import bp
from ...models import Student, VirtualMachine
from ...extensions import db
from ...services.proxmox_client import ProxmoxClient
import os
import uuid
from app.models import Submission

def student_required(f):
    """Decorator to require student login"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'student_id' not in session:
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('auth.student_login'))
        return f(*args, **kwargs)
    return decorated_function
@bp.route("/submit_screenshot", methods=["POST"])
@student_required
def submit_screenshot():

    student_id = session.get("student_id")
    if not student_id:
        return jsonify({"error": "Not logged in"}), 403

    student = Student.query.get(student_id)
    if not student:
        return jsonify({"error": "Invalid student"}), 403

    assignment_id = request.form.get("assignment_id")
    if not assignment_id:
        return jsonify({"error": "No assignment specified"}), 400

    from app.models import Assignment
    assignment = Assignment.query.get(assignment_id)
    if not assignment or assignment.classroom_id != student.classroom_id:
        return jsonify({"error": "Invalid assignment"}), 403

    image = request.files.get("image")
    if not image:
        return jsonify({"error": "No image uploaded"}), 400

    vm_id = request.form.get("vm_id")
    if vm_id:
        vm = VirtualMachine.query.get(vm_id)
        if not vm or vm.student_id != student.id:
            return jsonify({"error": "Invalid VM"}), 403

    filename = f"{uuid.uuid4()}.png"
    # Ensure directory exists
    upload_dir = "app/uploads/submissions"
    os.makedirs(upload_dir, exist_ok=True)
    path = os.path.join(upload_dir, filename)

    image.save(path)

    submission = Submission(
        student_id=student_id,
        assignment_id=assignment_id,
        vm_id=vm_id,
        image_path=path
    )

    db.session.add(submission)
    db.session.commit()

    return jsonify({"success": True})

@bp.route('/')
@student_required
def dashboard():
    """Student dashboard showing their VMs and assignments"""
    student_id = session.get('student_id')
    student = Student.query.get_or_404(student_id)
    
    # Get all VMs for this student
    vms = student.vms.all()
    
    # Get active and visible assignments for the student's classroom
    assignments = student.classroom.assignments.filter_by(is_visible=True).all()
    
    return render_template('student/dashboard.html', student=student, vms=vms, assignments=assignments)


@bp.route('/console/<int:vm_id>')
@student_required
def console(vm_id):
    """Embedded console view for a specific VM"""
    student_id = session.get('student_id')
    student = Student.query.get_or_404(student_id)
    
    # Get VM and verify it belongs to this student
    vm = VirtualMachine.query.get_or_404(vm_id)
    if vm.student_id != student.id:
        flash('Access denied', 'danger')
        return redirect(url_for('auth.student_login'))
    
    # Get active and visible assignments for the student's classroom
    assignments = student.classroom.assignments.filter_by(is_visible=True).all()
    
    # Pass VM info to template - WebSocket will be proxied through Flask
    return render_template('student/console.html', 
                         student=student, 
                         vm=vm,
                         node=vm.proxmox_node,
                         vmid=vm.proxmox_vmid,
                         assignments=assignments)

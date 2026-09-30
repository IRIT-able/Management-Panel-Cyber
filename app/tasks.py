from .extensions import scheduler, db
from .models import VirtualMachine, Student, Classroom
from .services.vm_orchestrator import deploy_vm_for_student, stop_vm_for_student, start_vm_for_student
from flask import current_app

def async_deploy_vms(student_ids, template_id, app_context):
    with app_context:
        for student_id in student_ids:
            try:
                deploy_vm_for_student(student_id, template_id)
            except Exception as e:
                current_app.logger.error(f"Failed to deploy VM for student {student_id}: {str(e)}")

def async_stop_all_vms(classroom_id, app_context):
    with app_context:
        classroom = Classroom.query.get(classroom_id)
        if not classroom: return
        for student in classroom.students:
            try:
                stop_vm_for_student(student.id)
            except Exception as e:
                current_app.logger.error(f"Failed to stop VM for student {student.id}: {str(e)}")

def async_start_all_vms(classroom_id, app_context):
    with app_context:
        classroom = Classroom.query.get(classroom_id)
        if not classroom: return
        for student in classroom.students:
            try:
                start_vm_for_student(student.id)
            except Exception as e:
                current_app.logger.error(f"Failed to start VM for student {student.id}: {str(e)}")

def scheduled_shutdown_all_vms(app_context):
    """Cron job to shut down all VMs outside school hours"""
    with app_context:
        vms = VirtualMachine.query.filter_by(status='running').all()
        for vm in vms:
            try:
                stop_vm_for_student(vm.student_id)
            except Exception as e:
                current_app.logger.error(f"Scheduled shutdown failed for VM {vm.id}: {str(e)}")

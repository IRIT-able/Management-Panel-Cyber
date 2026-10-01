#!/usr/bin/env python3
import os
import sys
import logging
import traceback
from app import create_app, db
from app.models import User, Student, VirtualMachine, Classroom
from app.services.proxmox_client import ProxmoxClient, get_proxmox_client
from app.services.vm_orchestrator import get_vm_status
import requests

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

def test_database(app):
    logging.info("Testing Database Connection...")
    with app.app_context():
        try:
            users_count = User.query.count()
            students_count = Student.query.count()
            vms_count = VirtualMachine.query.count()
            classrooms_count = Classroom.query.count()
            logging.info(f"✅ Database connected. Found: {users_count} Users, {classrooms_count} Classrooms, {students_count} Students, {vms_count} VMs.")
            return True
        except Exception as e:
            logging.error(f"❌ Database connection failed: {e}")
            return False

def test_proxmox_auth(app):
    logging.info("Testing Proxmox API Authentication...")
    with app.app_context():
        try:
            prox = get_proxmox_client()
            nodes = prox._api_request("GET", "/nodes")
            logging.info(f"✅ Proxmox API authenticated successfully. Found {len(nodes)} nodes.")
            return True
        except Exception as e:
            logging.error(f"❌ Proxmox API authentication failed: {e}")
            return False

def test_proxmox_vm_status(app):
    logging.info("Testing Proxmox VM Status Retrieval...")
    with app.app_context():
        try:
            # Get a VM from DB to test
            vm = VirtualMachine.query.first()
            if not vm:
                logging.warning("⚠️ No VMs found in database to test status.")
                return True
            
            logging.info(f"Testing status for VM {vm.proxmox_vmid} on node {vm.proxmox_node}...")
            status = get_vm_status(vm.proxmox_node, vm.proxmox_vmid)
            if 'status' in status or status.get('status') == 'deleted' or status.get('status') == 'unknown':
                 logging.info(f"✅ Successfully retrieved VM status: {status.get('status')}")
                 return True
            else:
                 logging.error(f"❌ VM status response invalid: {status}")
                 return False
        except Exception as e:
            logging.error(f"❌ Proxmox VM Status test failed: {e}")
            return False

def test_proxmox_vnc_ticket(app):
    logging.info("Testing Proxmox VNC Ticket Generation...")
    with app.app_context():
        try:
            vm = VirtualMachine.query.first()
            if not vm:
                logging.warning("⚠️ No VMs found in database to test VNC ticket.")
                return True
                
            prox = get_proxmox_client()
            ticket = prox.get_vnc_ticket(vm.proxmox_node, vm.proxmox_vmid)
            if 'ticket' in ticket and 'port' in ticket:
                logging.info(f"✅ Successfully generated VNC ticket and port for VM {vm.proxmox_vmid}.")
                return True
            else:
                logging.error(f"❌ VNC ticket response missing expected fields: {ticket}")
                return False
        except Exception as e:
            logging.error(f"❌ Proxmox VNC Ticket test failed: {e}")
            return False

def main():
    print("=======================================")
    print("🚀 CYBERLAB SYSTEM DIAGNOSTIC TOOL 🚀")
    print("=======================================")
    
    app = create_app()
    
    tests = [
        test_database,
        test_proxmox_auth,
        test_proxmox_vm_status,
        test_proxmox_vnc_ticket
    ]
    
    results = []
    for test in tests:
        results.append(test(app))
        print("-" * 40)
        
    print("\n=======================================")
    if all(results):
        print("✅ ALL TESTS PASSED! The Cyberlab is fully functional.")
        sys.exit(0)
    else:
        print(f"❌ {results.count(False)} TEST(S) FAILED. Please review the logs above.")
        sys.exit(1)

if __name__ == '__main__':
    main()

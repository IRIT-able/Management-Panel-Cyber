#!/usr/bin/env python3
"""
Clear Node Configuration Script
Removes all current node configurations, templates, and related data
to prepare for transfer to new Proxmox cluster
"""
import sqlite3
import os
import sys
from datetime import datetime

# Database path
DB_PATH = "instance/cyberlab.db"

def backup_database():
    """Create a backup before making changes"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"instance/cyberlab_backup_{timestamp}.db"
    
    try:
        import shutil
        shutil.copy2(DB_PATH, backup_path)
        print(f"✅ Database backed up to: {backup_path}")
        return backup_path
    except Exception as e:
        print(f"❌ Failed to create backup: {e}")
        return None

def clear_node_data():
    """Clear all node-related data from database"""
    print("\n🗑️  Clearing node configuration data...")
    
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Get current data counts for reporting
        cursor.execute("SELECT COUNT(*) FROM node_configurations")
        node_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM vm_templates")
        template_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM vm_template_replicas")
        replica_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM virtual_machines")
        vm_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM node_storage_configs")
        storage_count = cursor.fetchone()[0]
        
        print(f"📊 Current data:")
        print(f"   • Node configurations: {node_count}")
        print(f"   • VM templates: {template_count}")  
        print(f"   • Template replicas: {replica_count}")
        print(f"   • Virtual machines: {vm_count}")
        print(f"   • Storage configurations: {storage_count}")
        
        if vm_count > 0:
            print("\n⚠️  WARNING: There are active VMs in the database!")
            print("   This script will remove all VM records, but VMs may still exist in Proxmox.")
            response = input("   Continue anyway? (yes/no): ").lower().strip()
            if response != 'yes':
                print("❌ Operation cancelled")
                return False
        
        # Clear data in correct order (respecting foreign key constraints)
        print("\n🗑️  Removing data...")
        
        # 1. Clear VM template replicas
        cursor.execute("DELETE FROM vm_template_replicas")
        print(f"   ✅ Removed {replica_count} template replicas")
        
        # 2. Clear virtual machines
        cursor.execute("DELETE FROM virtual_machines")
        print(f"   ✅ Removed {vm_count} virtual machine records")
        
        # 3. Clear VM templates
        cursor.execute("DELETE FROM vm_templates")
        print(f"   ✅ Removed {template_count} VM templates")
        
        # 4. Clear node storage configurations
        cursor.execute("DELETE FROM node_storage_configs")
        print(f"   ✅ Removed {storage_count} storage configurations")
        
        # 5. Clear node configurations
        cursor.execute("DELETE FROM node_configurations")
        print(f"   ✅ Removed {node_count} node configurations")
        
        # Commit changes
        conn.commit()
        conn.close()
        
        print("\n✅ Successfully cleared all node configuration data!")
        print("🔄 You can now add nodes for your new Proxmox cluster")
        
        return True
        
    except Exception as e:
        print(f"❌ Error clearing data: {e}")
        if 'conn' in locals():
            conn.rollback()
            conn.close()
        return False

def main():
    """Main execution function"""
    print("🧹 CyberLab Admin Panel - Clear Node Configuration")
    print("=" * 50)
    
    # Check if database exists
    if not os.path.exists(DB_PATH):
        print(f"❌ Database not found at: {DB_PATH}")
        print("   Make sure you're running this from the cyberlab-admin directory")
        sys.exit(1)
    
    # Create backup
    backup_path = backup_database()
    if not backup_path:
        response = input("⚠️  Failed to create backup. Continue anyway? (yes/no): ").lower().strip()
        if response != 'yes':
            print("❌ Operation cancelled")
            sys.exit(1)
    
    # Confirm operation
    print("\n⚠️  This will remove ALL current node configurations, templates, and VMs!")
    print("   This prepares the system for transfer to a new Proxmox cluster.")
    response = input("   Are you sure you want to continue? (yes/no): ").lower().strip()
    
    if response == 'yes':
        success = clear_node_data()
        if success:
            print("\n📝 Next steps:")
            print("   1. Configure your new Proxmox cluster connection in .env file")
            print("   2. Add your new nodes via the admin panel: /admin/nodes/create") 
            print("   3. Upload/configure VM templates for the new cluster")
            print("   4. Test VM deployment on new nodes")
        else:
            if backup_path:
                print(f"\n🔄 To restore from backup if needed:")
                print(f"   cp {backup_path} {DB_PATH}")
    else:
        print("❌ Operation cancelled")

if __name__ == "__main__":
    main()
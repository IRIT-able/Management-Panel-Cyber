"""Proxmox VE SSH-based API client wrapper (avoids gevent SSL recursion)"""

import paramiko
import json
import threading
import time
import requests
from typing import Dict, List

# Global SSH connection pool to avoid resource exhaustion
_ssh_pool_lock = threading.Lock()
_ssh_connections = {}  # Key: (host, user, key_path) -> SSHClient
_ssh_timestamps = {}   # Track when connections were created


class ProxmoxClient:
    """Client for interacting with Proxmox VE via SSH commands."""

    def __init__(
        self,
        host: str,
        user: str = None,
        token_name: str = None,
        token_value: str = None,
        ssh_host: str = None,
        ssh_user: str = None,
        ssh_key_path: str = None,
        password: str = None
    ):
        self.host = host
        self.user = user
        self.token_name = token_name
        self.token_value = token_value
        self.password = password

        self.ssh_host = ssh_host
        self.ssh_user = ssh_user
        self.ssh_key_path = ssh_key_path

        self.use_ssh = bool(ssh_key_path and ssh_host)

        if not self.use_ssh:
            raise Exception("SSH configuration required (ssh_host and ssh_key_path)")

        self.session_ticket = None
        self.csrf_token = None
        
        # Auth cookie and CSRF token cache (per instance)
        self._auth_cookie = None
        self._csrf_token = None
        self._auth_cookie_time = 0

    def _get_ssh_connection(self):
        """Get or create an SSH connection from the pool"""
        global _ssh_connections, _ssh_timestamps, _ssh_pool_lock
        
        key = (self.ssh_host, self.ssh_user, self.ssh_key_path)
        
        with _ssh_pool_lock:
            # Check if we have a reusable connection
            if key in _ssh_connections:
                ssh = _ssh_connections[key]
                # Verify it's still alive with paramiko built-in checking
                try:
                    transport = ssh.get_transport()
                    if transport and transport.is_active():
                        return ssh
                    else:
                        raise Exception("Transport inactive")
                except Exception:
                    # Connection dead, remove it
                    try:
                        ssh.close()
                    except:
                        pass
                    del _ssh_connections[key]
                    if key in _ssh_timestamps:
                        del _ssh_timestamps[key]
            
            # Create new connection
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(
                self.ssh_host,
                username=self.ssh_user,
                key_filename=self.ssh_key_path,
                timeout=10
            )
            _ssh_connections[key] = ssh
            _ssh_timestamps[key] = time.time()
            return ssh

    def _ssh_command(self, command: str) -> str:
        """Execute command on Proxmox host via SSH"""
        try:
            ssh = self._get_ssh_connection()
            stdin, stdout, stderr = ssh.exec_command(command, timeout=15)
            out = stdout.read().decode()
            err = stderr.read().decode()

            if err.strip() and "warning" not in err.lower():
                raise Exception(err)
            return out
        except Exception as e:
            raise Exception(f"SSH error: {e}")

    def get_nodes(self) -> List[str]:
        """Get list of cluster nodes"""
        result = self._ssh_command("pvesh get /nodes --output-format=json")
        nodes = json.loads(result)
        return [n["node"] for n in nodes]

    def get_next_vmid(self) -> int:
        """Get next available VMID"""
        result = self._ssh_command("pvesh get /cluster/nextid")
        return int(result.strip())

    def get_vm_config(self, node: str, vmid: int) -> Dict:
        """Get VM configuration"""
        result = self._ssh_command(f"pvesh get /nodes/{node}/qemu/{vmid}/config --output-format=json")
        return json.loads(result)


    def _api_request(self, method: str, path: str, data: dict = None) -> dict:
        url = f"{self.host}/api2/json{path}"
        headers = {}
        if self.token_name and self.token_value:
            headers["Authorization"] = f"PVEAPIToken={self.user}!{self.token_name}={self.token_value}"
        else:
            auth_cookie = self.get_auth_cookie()
            csrf_token = self.get_csrf_token()
            headers['Cookie'] = f'PVEAuthCookie={auth_cookie}'
            headers['CSRFPreventionToken'] = csrf_token

        import requests
        if method.upper() == 'GET':
            response = requests.get(url, headers=headers, params=data, verify=False, timeout=10)
        elif method.upper() == 'POST':
            response = requests.post(url, headers=headers, data=data, verify=False, timeout=10)
        elif method.upper() == 'PUT':
            response = requests.put(url, headers=headers, data=data, verify=False, timeout=10)
        elif method.upper() == 'DELETE':
            response = requests.delete(url, headers=headers, data=data, verify=False, timeout=30)
        else:
            raise ValueError(f"Unsupported method {method}")
            
        response.raise_for_status()
        return response.json().get("data", {})

    def get_vm_status(self, node: str, vmid: int) -> Dict:
        """Get VM status"""
        return self._api_request("GET", f"/nodes/{node}/qemu/{vmid}/status/current")

    def start_vm(self, node: str, vmid: int):
        """Start a VM"""
        self._api_request("POST", f"/nodes/{node}/qemu/{vmid}/status/start")

    def stop_vm(self, node: str, vmid: int):
        """Stop a VM"""
        self._api_request("POST", f"/nodes/{node}/qemu/{vmid}/status/stop")

    def reset_vm(self, node: str, vmid: int):
        """Reset a VM"""
        self._api_request("POST", f"/nodes/{node}/qemu/{vmid}/status/reset")

    def suspend_vm(self, node: str, vmid: int):
        """Suspend a VM"""
        self._api_request("POST", f"/nodes/{node}/qemu/{vmid}/status/suspend")

    def resume_vm(self, node: str, vmid: int):
        """Resume a VM"""
        self._api_request("POST", f"/nodes/{node}/qemu/{vmid}/status/resume")

    def delete_vm(self, node: str, vmid: int):
        """Delete a VM"""
        self._api_request("DELETE", f"/nodes/{node}/qemu/{vmid}")

    def clone_vm(self, node: str, template_id: int, new_vmid: int, name: str,
                 storage: str = None, linked: bool = True) -> str:
        """Clone a VM from a template using Proxmox API"""
        url = f"{self.host}/api2/json/nodes/{node}/qemu/{template_id}/clone"
        
        data = {
            "newid": new_vmid,
            "name": name
        }
        
        # Add storage parameter if specified
        if storage:
            data["storage"] = storage
        
        # For linked clones (requires snapshot on template)
        if linked and storage:
            data["format"] = "qcow2"
        
        # Set up authentication headers
        headers = {}
        if self.token_name and self.token_value:
            headers["Authorization"] = f"PVEAPIToken={self.user}!{self.token_name}={self.token_value}"
        
        response = requests.post(url, data=data, headers=headers, verify=False, timeout=30)
        response.raise_for_status()
        
        return response.json().get('data', '')

    def optimize_vm_for_performance(self, node: str, vmid: int):
        """Optimize VM configuration for better performance"""
        cfg = self.get_vm_config(node, vmid)
        changes = {}
        
        # Enable QEMU guest agent
        if "agent" not in cfg:
            changes["agent"] = "enabled=1"
        
        # Use host CPU type for better performance
        if cfg.get("cpu") == "qemu64" or "cpu" not in cfg:
            changes["cpu"] = "kvm64"
        
        # Apply changes if any
        if changes:
            url = f"{self.host}/api2/json/nodes/{node}/qemu/{vmid}/config"
            
            headers = {}
            if self.token_name and self.token_value:
                headers["Authorization"] = f"PVEAPIToken={self.user}!{self.token_name}={self.token_value}"
            
            response = requests.put(url, data=changes, headers=headers, verify=False, timeout=30)
            response.raise_for_status()

    def get_console_url(self, node: str, vmid: int) -> str:
        """Generate VNC console URL for Proxmox web UI"""
        proxmox_host = self.host.replace('https://', '').replace('http://', '')
        return f"https://{proxmox_host}/?console=kvm&novnc=1&vmid={vmid}&node={node}&resize=off"

    def get_auth_cookie(self) -> str:
        """Get PVEAuthCookie using password authentication via API with caching"""
        if not self.password:
            raise Exception("Password required for WebSocket VNC authentication")
        
        # Return cached cookie if valid (good for ~2 hours, but cache for 30 mins)
        if self._auth_cookie and (time.time() - self._auth_cookie_time) < 1800:
            return self._auth_cookie
        
        # Use API to create access ticket (no SSH)
        url = f"{self.host}/api2/json/access/ticket"
        data = {
            "username": self.user,
            "password": self.password
        }
        
        response = requests.post(url, data=data, verify=False, timeout=10)
        response.raise_for_status()
        result = response.json()['data']
        
        self._auth_cookie = result.get("ticket", "")
        self._csrf_token = result.get("CSRFPreventionToken", "")
        self._auth_cookie_time = time.time()
        return self._auth_cookie
    
    def get_csrf_token(self) -> str:
        """Get CSRF token (call get_auth_cookie first to populate it)"""
        if not self._csrf_token:
            # Refresh auth which also gets CSRF token
            self.get_auth_cookie()
        return self._csrf_token
    
    def get_vnc_ticket(self, node: str, vmid: int) -> Dict:
        """Get VNC ticket for console access via API (preferring Token over Password)"""
        url = f"{self.host}/api2/json/nodes/{node}/qemu/{vmid}/vncproxy"
        
        headers = {}
        
        # Use API Token if available
        if self.token_name and self.token_value:
            headers['Authorization'] = f"PVEAPIToken={self.user}!{self.token_name}={self.token_value}"
        else:
            # Fallback to Password/Cookie auth
            auth_cookie = self.get_auth_cookie()
            csrf_token = self.get_csrf_token()
            headers['Cookie'] = f'PVEAuthCookie={auth_cookie}'
            headers['CSRFPreventionToken'] = csrf_token
        
        # Request a WebSocket-compatible ticket
        response = requests.post(url, headers=headers, data={'websocket': 1}, verify=False, timeout=10)
        response.raise_for_status()
        data = response.json()['data']
        
        return {
            "ticket": data.get("ticket", ""),
            "port": data.get("port", ""),
            "upid": data.get("upid", ""),
            "user": data.get("user", self.user), # Proxmox might return the user now
            "auth_cookie": None
        }

from flask import session, current_app
from urllib.parse import quote
from flask_login import current_user
from ...models import VirtualMachine
from ...services.proxmox_client import ProxmoxClient
import ssl
import websocket
import threading
import sys
import struct
from Crypto.Cipher import DES

# Simple in-memory cache for VNC tickets (vm_id -> {ticket, port, timestamp})
vnc_ticket_cache = {}

def register_websocket_routes(sock):
    """Register WebSocket routes with flask-sock instance"""
    
    @sock.route('/vnc-proxy/ws/<int:vm_id>')
    def vnc_websocket(ws, vm_id):
        """WebSocket proxy for VNC connections to Proxmox
        
        NOTE: This handler now just proxies binary data transparently.
        The VNC protocol negotiation (auth, compression, etc.) is handled
        entirely by noVNC (client) and Proxmox (server).
        """
        print(f"[VNC-PROXY] New WebSocket connection for VM {vm_id}", flush=True)
        from flask import request
        print(f"[VNC-PROXY] Request headers: {dict(request.headers)}", flush=True)
        
        try:
            # Verify access permissions
            try:
                vm = VirtualMachine.query.get_or_404(vm_id)
            except:
                print(f"[VNC-PROXY] VM {vm_id} not found", flush=True)
                ws.close(message='VM not found')
                return
            
            # Check access: student or teacher of the class
            from flask_login import current_user
            student_id = session.get('student_id')
            
            print(f"[VNC-PROXY] Access check - student_id: {student_id}, user: {current_user}, vm.student_id: {vm.student_id}", flush=True)
            
            # Allow access if:
            # 1. Student accessing their own VM, or
            # 2. Teacher/admin accessing a VM in their class
            has_access = False
            
            if student_id and vm.student_id == student_id:
                has_access = True
                print(f"[VNC-PROXY] Access granted - student accessing own VM", flush=True)
            elif current_user.is_authenticated:
                if current_user.is_admin():
                    has_access = True
                    print(f"[VNC-PROXY] Access granted - admin user", flush=True)
                else:
                    # Check if teacher owns the class this VM's student is in
                    try:
                        if vm.student and vm.student.classroom and vm.student.classroom.teacher_id == current_user.id:
                            has_access = True
                            print(f"[VNC-PROXY] Access granted - teacher of the class", flush=True)
                    except Exception as e:
                        print(f"[VNC-PROXY] Error checking teacher access: {e}", flush=True)
            
            if not has_access:
                print(f"[VNC-PROXY] Access denied for VM {vm_id}", flush=True)
                ws.close(message='Access denied')
                return
            
            print(f"[VNC-PROXY] Access granted for VM {vm_id}", flush=True)
            
            # Get VNC ticket from Proxmox via SSH
            proxmox = ProxmoxClient(
                host=current_app.config.get('PROXMOX_HOST'),
                user=current_app.config.get('PROXMOX_USER'),
                token_name=current_app.config.get('PROXMOX_TOKEN_NAME'),
                token_value=current_app.config.get('PROXMOX_TOKEN_VALUE'),
                password=current_app.config.get('PROXMOX_PASSWORD'),
                ssh_host=current_app.config.get('PROXMOX_SSH_HOST', '192.168.1.2'),
                ssh_user=current_app.config.get('PROXMOX_SSH_USER', 'root'),
                ssh_key_path=current_app.config.get('PROXMOX_SSH_KEY_PATH', '/home/admin/Admin-Panel/cyberlab-admin/id_rsa'),
            )
            
            proxmox_ws = None
            try:
                vnc_data = proxmox.get_vnc_ticket(vm.proxmox_node, vm.proxmox_vmid)
                print(f"[VNC-PROXY] Generated VNC ticket for VM {vm_id}", flush=True)
                
                # Get auth cookie for WebSocket (do this here, not in get_vnc_ticket)
                auth_cookie = None
                try:
                    auth_cookie = proxmox.get_auth_cookie()
                    print(f"[VNC-PROXY] Generated auth cookie: {auth_cookie[:30]}...", flush=True)
                except Exception as auth_err:
                    print(f"[VNC-PROXY] Warning: Could not get auth cookie: {auth_err}", flush=True)
                
                # Build Proxmox WebSocket URL with vncticket as query parameter
                proxmox_host = (current_app.config.get('PROXMOX_HOST') or '').replace('https://', '').replace('http://', '')
                proxmox_ws_url = f"wss://{proxmox_host}/api2/json/nodes/{vm.proxmox_node}/qemu/{vm.proxmox_vmid}/vncwebsocket"
                proxmox_ws_url += f"?port={vnc_data['port']}&vncticket={quote(vnc_data['ticket'], safe='')}"
                
                print(f"[VNC-PROXY] Proxmox WebSocket URL: {proxmox_ws_url[:100]}...", flush=True)
                
                # Connect to Proxmox WebSocket with proper settings
                try:
                    # Disable compression in the WebSocket connection to Proxmox
                    # This is critical - Proxmox uses permessage-deflate which causes "rsv is not implemented"
                    proxmox_ws = websocket.WebSocket(
                        sslopt={"cert_reqs": ssl.CERT_NONE},
                        enable_multithread=False
                    )
                    
                    headers = []
                    
                    # Use API Token for WebSocket if configured
                    if proxmox.token_name and proxmox.token_value:
                        auth_header = f"PVEAPIToken={proxmox.user}!{proxmox.token_name}={proxmox.token_value}"
                        headers.append(f"Authorization: {auth_header}")
                        print(f"[VNC-PROXY] Connecting with API Token...", flush=True)
                    elif auth_cookie:
                        headers.append(f"Cookie: PVEAuthCookie={auth_cookie}")
                        print(f"[VNC-PROXY] Connecting with auth cookie...", flush=True)
                    else:
                        print(f"[VNC-PROXY] WARNING: No auth method available...", flush=True)
                    
                    # Connect to Proxmox with compression disabled
                    proxmox_ws.connect(
                        proxmox_ws_url, 
                        header=headers,
                        compression=None  # Disable compression
                    )
                    # Set timeout high enough to not interfere with normal operation
                    proxmox_ws.settimeout(30)
                    print(f"[VNC-PROXY] Connected to Proxmox successfully", flush=True)
                except Exception as ws_err:
                    print(f"[VNC-PROXY] FATAL: Failed to connect to Proxmox: {type(ws_err).__name__}: {ws_err}", flush=True)
                    ws.close(message=f'Failed to connect to Proxmox: {str(ws_err)[:100]}')
                    return
                
                # === CRITICAL: Perform VNC Authentication with Proxmox ===
                # The ticket must be used as the VNC password, not as a pre-auth token
                print(f"[VNC-PROXY] Performing VNC authentication with ticket...", flush=True)
                
                try:
                    import struct
                    from Crypto.Cipher import DES
                    
                    # 1. Read RFB protocol version
                    version = proxmox_ws.recv()
                    print(f"[VNC-PROXY] Proxmox VNC version: {version}", flush=True)
                    
                    # 2. Send back the same version
                    proxmox_ws.send(version, opcode=websocket.ABNF.OPCODE_BINARY)
                    
                    # 3. Read security types
                    sec_data = proxmox_ws.recv()
                    if not sec_data or len(sec_data) < 1:
                        raise Exception("No security types received")
                    
                    count = sec_data[0]
                    types = sec_data[1:1+count] if len(sec_data) > 1 else []
                    print(f"[VNC-PROXY] Security types offered: {list(types)}", flush=True)
                    
                    if 2 not in types:
                        raise Exception(f"VncAuth (type 2) not offered. Available: {list(types)}")
                    
                    # 4. Select VncAuth (type 2)
                    proxmox_ws.send(bytes([2]), opcode=websocket.ABNF.OPCODE_BINARY)
                    
                    # 5. Receive challenge (16 bytes)
                    challenge = proxmox_ws.recv()
                    if len(challenge) != 16:
                        raise Exception(f"Expected 16-byte challenge, got {len(challenge)} bytes")
                    
                    print(f"[VNC-PROXY] Received VNC auth challenge", flush=True)
                    
                    # 6. Encrypt challenge using ticket as password
                    # VNC uses first 8 bytes of password, DES-ECB with bit-reversed key
                    ticket = vnc_data['ticket']
                    password = ticket[:8].encode() if isinstance(ticket, str) else ticket[:8]
                    key = (password + b'\x00' * 8)[:8]  # Pad/truncate to 8 bytes
                    
                    # Reverse bits in each byte (VNC requirement)
                    def reverse_bits(byte):
                        result = 0
                        for i in range(8):
                            result = (result << 1) | ((byte >> i) & 1)
                        return result
                    
                    key_reversed = bytes([reverse_bits(b) for b in key])
                    des = DES.new(key_reversed, DES.MODE_ECB)
                    response = des.encrypt(challenge)
                    
                    # 7. Send encrypted response
                    proxmox_ws.send(response, opcode=websocket.ABNF.OPCODE_BINARY)
                    
                    # 8. Check auth result
                    auth_result = proxmox_ws.recv()
                    if len(auth_result) < 4:
                        raise Exception("Invalid auth response")
                    
                    status = struct.unpack('>I', auth_result[:4])[0]
                    if status != 0:
                        reason = "Authentication failed"
                        if len(auth_result) >= 8:
                            reason_len = struct.unpack('>I', auth_result[4:8])[0]
                            if len(auth_result) >= 8 + reason_len:
                                reason = auth_result[8:8+reason_len].decode('utf-8', errors='ignore')
                        raise Exception(f"VNC auth failed: {reason}")
                    
                    print(f"[VNC-PROXY] ✅ VNC authentication successful!", flush=True)
                    
                    # Now we need to replay the RFB handshake to the client
                    # The client (noVNC) expects to see:
                    # 1. RFB version
                    # 2. Security types (but we'll say "None" since we already authed)
                    # 3. Security result success
                    
                    print(f"[VNC-PROXY] Sending RFB handshake to client...", flush=True)
                    
                    # Send RFB version to client
                    ws.send(version)
                    
                    # Wait for client's version response
                    client_version = ws.receive()
                    print(f"[VNC-PROXY] Client version: {client_version}", flush=True)
                    
                    # Send security type 1 (None) to client since we already authenticated
                    ws.send(bytes([1, 1]))  # 1 type, type=1 (None)
                    
                    # Wait for client to select security type
                    client_sec_choice = ws.receive()
                    
                    #Send security result: 0 = success
                    ws.send(struct.pack('>I', 0))
                    
                    print(f"[VNC-PROXY] RFB handshake with client complete", flush=True)
                    
                except Exception as auth_err:
                    print(f"[VNC-PROXY] VNC authentication error: {auth_err}", flush=True)
                    try:
                        proxmox_ws.close()
                    except:
                        pass
                    ws.close(message=f'VNC authentication failed: {str(auth_err)[:100]}')
                    return
                
                print(f"[VNC-PROXY] Starting bidirectional proxy for VM {vm_id}", flush=True)
                
                # Forward from client to Proxmox
                def client_to_proxmox():
                    print(f"[VNC-PROXY] Client->Proxmox thread started", flush=True)
                    try:
                        msg_count = 0
                        while True:
                            try:
                                data = ws.receive()
                                if data is None:
                                    print(f"[VNC-PROXY] Client closed connection (receive returned None)", flush=True)
                                    break
                            except Exception as recv_err:
                                print(f"[VNC-PROXY] Error receiving from client: {type(recv_err).__name__}: {recv_err}", flush=True)
                                break
                            
                            msg_count += 1
                            if msg_count <= 5:
                                print(f"[VNC-PROXY] C->P msg #{msg_count}: {len(data)} bytes: {data[:10]}", flush=True)
                            
                            # Forward to Proxmox
                            try:
                                proxmox_ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY if isinstance(data, bytes) else websocket.ABNF.OPCODE_TEXT)
                            except Exception as send_err:
                                print(f"[VNC-PROXY] Error sending to Proxmox: {type(send_err).__name__}: {send_err}", flush=True)
                                break
                    except Exception as e:
                        print(f"[VNC-PROXY] Client->Proxmox thread error: {e}", flush=True)
                    finally:
                        print(f"[VNC-PROXY] Client->Proxmox thread ending", flush=True)
                        try:
                            if proxmox_ws:
                                proxmox_ws.close()
                        except:
                            pass
                
                # Start client->proxmox thread
                thread = threading.Thread(target=client_to_proxmox, daemon=True)
                thread.start()
                print(f"[VNC-PROXY] Client->Proxmox forwarding thread started", flush=True)
                
                # Forward from Proxmox to client (main thread)
                msg_count = 0
                print(f"[VNC-PROXY] Waiting for data from Proxmox...", flush=True)
                try:
                    while True:
                        try:
                            data = proxmox_ws.recv()
                            if not data:
                                print(f"[VNC-PROXY] Proxmox closed connection (recv returned empty)", flush=True)
                                break
                        except websocket.WebSocketConnectionClosedException:
                            print(f"[VNC-PROXY] Proxmox WebSocket connection closed", flush=True)
                            break
                        except Exception as recv_err:
                            print(f"[VNC-PROXY] Error receiving from Proxmox: {type(recv_err).__name__}: {recv_err}", flush=True)
                            break
                        
                        msg_count += 1
                        if msg_count <= 3:
                            print(f"[VNC-PROXY] P->C msg #{msg_count}: {len(data)} bytes", flush=True)
                        
                        # Forward everything to client
                        try:
                            ws.send(data)
                        except Exception as send_err:
                            print(f"[VNC-PROXY] Error sending data to client: {type(send_err).__name__}: {send_err}", flush=True)
                            break
                except Exception as e:
                    if "timed out" not in str(e).lower():
                        print(f"[VNC-PROXY] Proxmox->Client error: {type(e).__name__}: {e}", flush=True)
                finally:
                    print(f"[VNC-PROXY] Proxmox->Client loop ending", flush=True)

            except Exception as e:
                print(f"[VNC-PROXY] Error: {e}")
                import traceback
                traceback.print_exc()
            finally:
                print(f"[VNC-PROXY] Closing connection for VM {vm_id}")
                if proxmox_ws:
                    try:
                        proxmox_ws.close()
                    except:
                        pass
        
        except Exception as e:
            print(f"[VNC-PROXY] WebSocket handler error: {e}", flush=True)
            import traceback
            traceback.print_exc()
            try:
                ws.close(message='Internal error')
            except:
                pass



    @sock.route('/pc-proxy/ws/<pc_name>')
    def pc_vnc_proxy(ws, pc_name):
        print(f"[{pc_name}] WS CONNECTION RECEIVED", flush=True)
        import socket
        import threading
        
        from app.blueprints.teacher.routes import parse_ansible_inventory
        pcs = parse_ansible_inventory()
        ip = None
        for p in pcs:
            if p['name'] == pc_name:
                ip = p['ip']
                break
                
        if not ip:
            print(f"[{pc_name}] IP not found", flush=True)
            ws.close()
            return
            
        print(f"[{pc_name}] Connecting to VNC {ip}:5900...", flush=True)
        
        pc_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        pc_sock.settimeout(5.0)
        try:
            pc_sock.connect((ip, 5900))
            pc_sock.settimeout(None)
            print(f"[{pc_name}] TCP Connected to VNC {ip}:5900", flush=True)
        except Exception as e:
            print(f"[{pc_name}] Failed to connect to VNC: {e}", flush=True)
            ws.close()
            return
            
        def client_to_pc():
            print(f"[{pc_name}] Client->PC thread started", flush=True)
            try:
                while True:
                    data = ws.receive()
                    if data is None:
                        print(f"[{pc_name}] Client disconnected (receive None)", flush=True)
                        break
                    print(f"[{pc_name}] C->P: {len(data)} bytes: {data[:20]}", flush=True)
                    if isinstance(data, str):
                        data = data.encode('utf-8')
                    pc_sock.sendall(data)
            except Exception as e:
                print(f"[{pc_name}] C->P error: {e}", flush=True)
            finally:
                print(f"[{pc_name}] C->P thread ending", flush=True)
                pc_sock.close()
                
        thread = threading.Thread(target=client_to_pc, daemon=True)
        thread.start()
        
        print(f"[{pc_name}] Waiting for PC->Client data...", flush=True)
        try:
            while True:
                data = pc_sock.recv(4096)
                if not data:
                    print(f"[{pc_name}] PC disconnected (recv empty)", flush=True)
                    break
                print(f"[{pc_name}] P->C: {len(data)} bytes: {data[:20]}", flush=True)
                ws.send(data)
        except Exception as e:
            print(f"[{pc_name}] P->C error: {e}", flush=True)
        finally:
            print(f"[{pc_name}] P->C loop ending", flush=True)
            pc_sock.close()




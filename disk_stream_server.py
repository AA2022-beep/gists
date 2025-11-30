import socket
import subprocess
import os
import sys
import time
import json
import struct

# --- CONFIGURATION ---
HOST = '0.0.0.0'
PORT = 4443       # The port your SSL tunnel forwards to
DISK_DEVICE = '/dev/vda' # ⚠️ VERIFY THIS PATH ON YOUR DIGITAL OCEAN DROPLET!
SECRET_AUTH_TOKEN = 'your_super_secret_clone_key_12345' # ⚠️ CHANGE THIS!
REQUEST_METHOD = 'clone_disk'

# --- Protocol Helpers ---
BUFFER_SIZE = 8192
MAX_HEADER_SIZE = 4096

def receive_all(sock, num_bytes):
    """Helper function to guarantee reception of exactly num_bytes."""
    buffer = b''
    while len(buffer) < num_bytes:
        bytes_to_read = num_bytes - len(buffer)
        chunk = sock.recv(min(bytes_to_read, BUFFER_SIZE)) 
        
        if not chunk:
            # Connection closed prematurely
            return None
        buffer += chunk
    return buffer

def recv_header(sock):
    """Receives a 4-byte prefix indicating the size of the subsequent JSON data."""
    # Length is always 4 bytes
    length_prefix = receive_all(sock, 4)
    if length_prefix is None:
        return None
    # Unpack the 4-byte Big-endian integer
    return struct.unpack('!I', length_prefix)[0]

def send_error(sock, message):
    """Sends a standardized JSON error message back to the client."""
    error_response = json.dumps({"error": message, "result": None}).encode('utf-8')
    # Prefix the response with its 4-byte length
    length_prefix = struct.pack('!I', len(error_response))
    try:
        sock.sendall(length_prefix)
        sock.sendall(error_response)
        print(f"Sent error response: {message}")
    except Exception as e:
        print(f"Failed to send error response: {e}")

# --- MAIN SERVER CLASS ---

class DiskStreamerServer:
    
    def __init__(self, host, port, disk_device, auth_token):
        """Initializes server configuration."""
        self.host = host
        self.port = port
        self.disk_device = disk_device
        self.auth_token = auth_token

    def _auth_and_process_request(self, client_sock):
        """Receives the length-prefixed JSON request and validates it."""
        try:
            # 1. Receive the 4-byte length prefix
            json_len = recv_header(client_sock)
            if json_len is None:
                print("Client disconnected during header read.")
                return False
                
            if json_len > MAX_HEADER_SIZE:
                 send_error(client_sock, "Request header too large.")
                 return False

            # 2. Receive the JSON payload
            json_data = receive_all(client_sock, json_len)
            if json_data is None:
                print("Client disconnected during payload read.")
                return False
                
            request = json.loads(json_data.decode('utf-8'))
            
            # 3. Validate structure (must include method and token)
            if not all(k in request for k in ['method', 'token']):
                send_error(client_sock, "Invalid request format.")
                return False

            # 4. Validate method and token
            if request['method'] != REQUEST_METHOD:
                send_error(client_sock, f"Unsupported method: {request['method']}")
                return False
                
            if request['token'] != self.auth_token:
                send_error(client_sock, "Unauthorized: Invalid secret key.")
                return False

            # 5. Send success acknowledgment (crucial before streaming starts)
            success_response = json.dumps({"result": "Streaming started", "error": None}).encode('utf-8')
            length_prefix = struct.pack('!I', len(success_response))
            client_sock.sendall(length_prefix)
            client_sock.sendall(success_response)

            print(f"✅ Auth successful for method '{request['method']}'. Starting raw stream...")
            return True

        except Exception as e:
            print(f"Error during request processing: {e}")
            send_error(client_sock, "Internal server error during request processing.")
            return False

    def _stream_disk_content(self, client_sock):
        """Executes 'dd' and streams its raw output to the client."""
        if os.geteuid() != 0:
            print("❌ ERROR: Stream failed. Not running as root.")
            return

        command = ['dd', 'if=' + self.disk_device, 'bs=1M', 'status=none']
        
        try:
            # Start dd process
            dd_process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )

            chunk_size = 65536
            bytes_sent = 0
            start_time = time.time()
            
            # Start Streaming Loop
            while True:
                data = dd_process.stdout.read(chunk_size)
                if not data:
                    break
                
                # Send raw disk bytes directly
                client_sock.sendall(data) 
                bytes_sent += len(data)

                if bytes_sent % (100 * 1024 * 1024) == 0: 
                    elapsed = time.time() - start_time
                    speed = (bytes_sent / (1024*1024)) / elapsed if elapsed > 0 else 0
                    sys.stdout.write(f"Streaming... Sent: {bytes_sent / (1024*1024*1024):.2f} GB | Speed: {speed:.2f} MB/s\r")
                    sys.stdout.flush()

            dd_process.wait()
            
            if dd_process.returncode != 0:
                error_output = dd_process.stderr.read().decode('utf-8', errors='ignore')
                print(f"\n‼️ dd failed: {error_output}")
            else:
                print(f"\n✅ Stream complete. Total size sent: {bytes_sent / (1024*1024*1024):.2f} GB")

        except Exception as e:
            print(f"\nFATAL STREAMING ERROR: {e}")
            
        finally:
            # CRITICAL: Close the socket to signal End-Of-File (EOF) to the client.
            client_sock.close()
            print("Connection closed.")

    def start(self):
        """Sets up the TCP listener and handles incoming connections."""
        if os.geteuid() != 0:
            print("❌ ERROR: This server must be run with root privileges.")
            sys.exit(1)
            
        print(f"Starting plain TCP server on {self.host}:{self.port}...")
        
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((self.host, self.port))
        server_socket.listen(5)
        
        print(f"TCP socket listening. Awaiting client connection...")
        
        while True:
            client_sock = None
            try:
                # Accept the plain TCP connection
                client_sock, client_addr = server_socket.accept()
                print(f"\nIncoming connection from {client_addr}")
                
                # Process the JSON request and authenticate
                if self._auth_and_process_request(client_sock):
                    # Start raw binary streaming immediately after the JSON ACK
                    self._stream_disk_content(client_sock)
                else:
                    # If auth fails, close the socket (handled by finally block)
                    pass
                    
            except ConnectionResetError:
                print("Client forcibly closed the connection.")
            except Exception as e:
                print(f"An unexpected error occurred: {e}")
            finally:
                if client_sock:
                    try: client_sock.close() 
                    except: pass

# --- EXECUTION ---
if __name__ == '__main__':
    # Initial check for root is done in the start method, but good to check early too.
    if os.geteuid() != 0:
        print("Please run this script with 'sudo'.")
    
    server = DiskStreamerServer(
        host=HOST,
        port=PORT,
        disk_device=DISK_DEVICE,
        auth_token=SECRET_AUTH_TOKEN
    )
    server.start()

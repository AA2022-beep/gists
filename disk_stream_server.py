import socket
import ssl
import subprocess
import os
import sys
import struct
import time
import json

# --- CONFIGURATION ---
HOST = '0.0.0.0'  # Listen on all interfaces
PORT = 4443       # Choose a high port for security
DISK_DEVICE = '/dev/sda1' # ⚠️ VERIFY THIS PATH ON YOUR DIGITAL OCEAN DROPLET!
SECRET_AUTH_TOKEN = '!coxYocvmhLMhRsn=30a0a5@' # ⚠️ CHANGE THIS!

# --- CONSTANTS ---
REQUEST_METHOD = 'CLONE_DISK'
HEADER_SEPARATOR = b'\n'

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


class DiskStreamerServer:
    
    def __init__(self, host, port, certfile, keyfile, disk_device, auth_token):
        """Initializes the server with networking and security parameters."""
        self.host = host
        self.port = port
        self.certfile = certfile
        self.keyfile = keyfile
        self.disk_device = disk_device
        self.auth_token = auth_token
        self.server_socket = None

    def _process_json_request(self, client_sock, json_data):
        """Receives the length-prefixed JSON request and validates it."""
        try:
          
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

    def _auth_and_process_request(self, ssl_sock):
        """
        Receives the client request, validates the command and authentication.
        """
        print("Waiting for client command...")
        
        json_len = recv_header(ssl_sock)

        if json_len is None:
            print("Client disconnected during header read.")
            return False
                
        if json_len > MAX_HEADER_SIZE:
            send_error(ssl_sock, "Request header too large.")
            return False
                
        # 1. Receive the request (The Header)
        json_data = receive_all(ssl_sock, json_len)
        

        if not self._process_json_request(ssl_sock, json_data) :
            print("Client disconnected during payload read.")
            return False
                
        return True
            
        
    def _stream_disk_content(self, ssl_sock):
        """
        Executes 'dd' and streams its output to the client via SSL socket.
        """
        print(f"Starting disk stream of {self.disk_device}...")
        
        # Notify the client that streaming is starting (a simple ACK)
        ssl_sock.sendall(b'ACK:STREAMING_START\n')
        
        # Use 'dd' to read the raw device. 'bs=1M' for speed.
        command = ['dd', 'if=' + self.disk_device, 'bs=1M', 'status=none']
        
        try:
            dd_process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            chunk_size = 65536  # 64 KB chunk for socket efficiency
            bytes_sent = 0
            start_time = time.time()
            
            while True:
                data = dd_process.stdout.read(chunk_size)
                if not data:
                    break
                
                ssl_sock.sendall(data)
                bytes_sent += len(data)

                # Basic progress indication
                if bytes_sent % (50 * 1024 * 1024) == 0: # Every 50 MB
                    elapsed = time.time() - start_time
                    speed = (bytes_sent / (1024*1024)) / elapsed if elapsed > 0 else 0
                    sys.stdout.write(f"Streaming... Sent: {bytes_sent / (1024*1024*1024):.2f} GB | Speed: {speed:.2f} MB/s\r")
                    sys.stdout.flush()

            dd_process.wait()
            
            if dd_process.returncode != 0:
                error_output = dd_process.stderr.read().decode('utf-8', errors='ignore')
                print(f"\nERROR: dd command failed (Code: {dd_process.returncode}): {error_output}")
            else:
                print(f"\n✅ Stream complete. Total bytes sent: {bytes_sent} ({bytes_sent / (1024*1024*1024):.2f} GB)")

        except Exception as e:
            print(f"\nFATAL STREAMING ERROR: {e}")
            
        finally:
            # Crucial: Close the socket to signal EOF to the client.
            ssl_sock.close()
            print("Connection closed.")


    def start(self):
        """Sets up the TCP listener, wraps it with SSL, and handles connections."""
        # Check for root privilege
        if os.geteuid() != 0:
            print("❌ ERROR: This server must be run with root privileges to access the disk device.")
            sys.exit(1)
            
        print(f"Attempting to start server on {self.host}:{self.port}...")
        
        # 1. Create the plain TCP socket
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(5)
        
        print(f"TCP socket listening. Awaiting SSL client connection...")
        
        # 2. Prepare the SSL context
        # 
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(certfile=self.certfile, keyfile=self.keyfile)
        
        while True:
            client_sock = None
            ssl_sock = None
            try:
                # 3. Accept the plain TCP connection
                client_sock, client_addr = self.server_socket.accept()
                print(f"\nIncoming connection from {client_addr}")
                
                # 4. Wrap the socket for SSL handshake (Non-blocking by default)
                ssl_sock = ssl_context.wrap_socket(client_sock, server_side=True)
                print("SSL handshake successful.")

                # 5. Authenticate and process request
                if self._auth_and_process_request(ssl_sock):
                    # 6. If authenticated, start streaming
                    self._stream_disk_content(ssl_sock)
                else:
                    # If auth fails, close the socket quickly
                    print("Request rejected. Closing connection.")
                    ssl_sock.close()
                    
            except ssl.SSLError as e:
                print(f"SSL Error during connection: {e}. Closing socket.")
            except ConnectionResetError:
                print("Client reset the connection.")
            except Exception as e:
                print(f"An unexpected error occurred: {e}")
            except KeyboardInterrupt:
                print(f"Operation terminated by user! Exiting..")
            finally:
                if ssl_sock:
                    try: ssl_sock.close() 
                    except: pass
                elif ssl_sock:
                    try: ssl_sock.close() 
                    except: pass

# --- EXECUTION ---
if __name__ == '__main__':
    # Ensure you have server.crt and server.key in the same directory,
    # or provide the full paths.
    try:
        server = DiskStreamerServer(
            host=HOST,
            port=PORT,
            certfile='fullchain.pem', 
            keyfile='privkey.pem',
            disk_device=DISK_DEVICE,
            auth_token=SECRET_AUTH_TOKEN
        )
        server.start()
    except FileNotFoundError:
        print("\FATAL: SSL certificate or key file not found. Ensure 'server.crt' and 'server.key' exist.")
        sys.exit(1)

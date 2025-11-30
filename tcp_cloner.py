import socket
import ssl
import subprocess
import os
import sys
import struct
import time

# --- CONFIGURATION ---
HOST = '0.0.0.0'  # Listen on all interfaces
PORT = 4443       # Choose a high port for security
DISK_DEVICE = '/dev/vda' # ⚠️ VERIFY THIS PATH ON YOUR DIGITAL OCEAN DROPLET!
SECRET_AUTH_TOKEN = 'your_super_secret_clone_key_12345' # ⚠️ CHANGE THIS!

# --- CONSTANTS ---
REQUEST_COMMAND = 'CLONE_DISK'
HEADER_SEPARATOR = b'\n'
BUFFER_SIZE = 8192

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

    def _auth_and_process_request(self, ssl_sock):
        """
        Receives the client request, validates the command and authentication.
        """
        print("Waiting for client command...")
        
        # 1. Receive the request (up to the HEADER_SEPARATOR)
        request_bytes = b''
        try:
            while HEADER_SEPARATOR not in request_bytes:
                chunk = ssl_sock.recv(BUFFER_SIZE)
                if not chunk:
                    raise ConnectionResetError("Client disconnected before sending request.")
                request_bytes += chunk
        except Exception as e:
            print(f"Error receiving request: {e}")
            return False

        # 2. Extract and validate the header components
        try:
            header_str = request_bytes.split(HEADER_SEPARATOR, 1)[0].decode('utf-8').strip()
            
            # Expected format: "COMMAND:TOKEN"
            command, token = header_str.split(':', 1)
        except ValueError:
            print(f"Auth failed: Malformed request header: {header_str}")
            return False

        # 3. Check authentication and command
        if command != REQUEST_COMMAND:
            print(f"Auth failed: Unknown command received: {command}")
            return False
            
        if token != self.auth_token:
            print(f"Auth failed: Invalid token received: {token}")
            # Optionally send an error message back here
            ssl_sock.sendall(b'ERROR: UNAUTHORIZED\n')
            return False

        print(f"✅ Authentication successful. Command: {command}")
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
            finally:
                if ssl_sock:
                    try: ssl_sock.close() 
                    except: pass
                elif client_sock:
                    try: client_sock.close() 
                    except: pass

# --- EXECUTION ---
if __name__ == '__main__':
    # Ensure you have server.crt and server.key in the same directory,
    # or provide the full paths.
    try:
        server = DiskStreamerServer(
            host=HOST,
            port=PORT,
            certfile='server.crt', 
            keyfile='server.key',
            disk_device=DISK_DEVICE,
            auth_token=SECRET_AUTH_TOKEN
        )
        server.start()
    except FileNotFoundError:
        print("\nFATAL: SSL certificate or key file not found. Ensure 'server.crt' and 'server.key' exist.")
        sys.exit(1)

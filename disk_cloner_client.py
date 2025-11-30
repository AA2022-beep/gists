import socket
import ssl
import json
import struct
import sys
import time
import os

# --- CONFIGURATION (Client Side) ---
SERVER_HOST = 'your_digital_ocean_ip'  # ⚠️ REPLACE with your Droplet's IP or hostname
SERVER_PORT = 4443                      # ⚠️ Use the port exposed by your SSL client/forwarder
LOCAL_OUTPUT_FILE = 'cloned_server_disk.img'
AUTH_TOKEN = 'your_super_secret_clone_key_12345' # ⚠️ MUST MATCH the server's token
SERVER_CERT_PATH = 'server.crt'         # Path to the server's public certificate
REQUEST_METHOD = 'clone_disk'

# --- Protocol Helpers ---
BUFFER_SIZE = 8192

def receive_all(sock, num_bytes):
    """Helper function to guarantee reception of exactly num_bytes."""
    buffer = b''
    while len(buffer) < num_bytes:
        bytes_to_read = num_bytes - len(buffer)
        
        # Read from the secure socket
        chunk = sock.recv(min(bytes_to_read, BUFFER_SIZE)) 
        
        if not chunk:
            return None # Connection closed prematurely
        buffer += chunk
    return buffer

def recv_response(ssl_sock):
    """Receives the 4-byte length prefix and the subsequent JSON response."""
    # 1. Receive the 4-byte length prefix
    length_prefix = receive_all(ssl_sock, 4)
    if length_prefix is None:
        return None
    
    # 2. Unpack the length
    json_len = struct.unpack('!I', length_prefix)[0]
    
    # 3. Receive the JSON payload
    json_data = receive_all(ssl_sock, json_len)
    if json_data is None:
        return None
        
    return json.loads(json_data.decode('utf-8'))


class DiskCloneClient:
    
    def __init__(self, host, port, certfile, auth_token, output_file):
        self.host = host
        self.port = port
        self.certfile = certfile
        self.auth_token = auth_token
        self.output_file = output_file

    def run(self):
        """Establishes the SSL connection, sends the request, and receives the stream."""
        
        # 1. Set up SSL Context
        # We verify the server's certificate against the one we trust (server.crt)
        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=self.certfile)
        
        # 2. Create and connect the plain socket
        plain_sock = socket.create_connection((self.host, self.port))
        
        # 3. Wrap the socket for SSL handshake
        try:
            # Note: server_hostname is critical for certificate verification
            ssl_sock = ssl_context.wrap_socket(plain_sock, server_hostname=self.host)
        except ssl.SSLError as e:
            print(f"❌ SSL Handshake Failed: {e}")
            plain_sock.close()
            return

        print(f"✅ Connection established to {self.host}:{self.port}")
        
        # 4. Send the JSON-RPC Request
        request = {
            "method": REQUEST_METHOD,
            "token": self.auth_token,
            "id": 1
        }
        json_data = json.dumps(request).encode('utf-8')
        length_prefix = struct.pack('!I', len(json_data))
        
        try:
            ssl_sock.sendall(length_prefix)
            ssl_sock.sendall(json_data)
            print("Request sent. Awaiting server acknowledgment...")
            
            # 5. Receive and parse the ACK/Error response
            response = recv_response(ssl_sock)
            
            if response is None:
                print("Server closed connection before response received.")
                return

            if response.get("error"):
                print(f"❌ Server Error: {response['error']}")
                return

            if response.get("result") == "Streaming started":
                print("✅ Server ACK received. Starting disk data download.")
            else:
                print(f"Server sent unexpected response: {response}")
                return

            # 6. Start Receiving Raw Disk Stream
            self._receive_disk_stream(ssl_sock)

        except Exception as e:
            print(f"FATAL Client Error: {e}")
            
        finally:
            ssl_sock.close()
            print("Connection closed.")

    def _receive_disk_stream(self, ssl_sock):
        """Continuously reads raw bytes and writes them to the output file."""
        bytes_received = 0
        start_time = time.time()
        
        try:
            with open(self.output_file, 'wb') as f:
                while True:
                    # Read maximum chunk size from the socket
                    data = ssl_sock.recv(BUFFER_SIZE)
                    
                    if not data:
                        # Server closed connection, stream is complete
                        break
                    
                    f.write(data)
                    bytes_received += len(data)

                    # Progress indicator
                    if bytes_received % (100 * 1024 * 1024) == 0: # Every 100 MB
                        elapsed = time.time() - start_time
                        speed = (bytes_received / (1024*1024)) / elapsed if elapsed > 0 else 0
                        sys.stdout.write(f"Receiving... Received: {bytes_received / (1024*1024*1024):.2f} GB | Speed: {speed:.2f} MB/s\r")
                        sys.stdout.flush()

            print(f"\n✅ Download complete. Total size: {bytes_received / (1024*1024*1024):.2f} GB written to {self.output_file}")
            
        except Exception as e:
            print(f"\n‼️ Error during data reception: {e}")
        # 


# --- EXECUTION ---
if __name__ == '__main__':
    if not os.path.exists(SERVER_CERT_PATH):
        print(f"FATAL: SSL certificate '{SERVER_CERT_PATH}' not found. You must use the server's public certificate.")
        sys.exit(1)
        
    client = DiskCloneClient(
        host=SERVER_HOST,
        port=SERVER_PORT,
        certfile=SERVER_CERT_PATH,
        auth_token=AUTH_TOKEN,
        output_file=LOCAL_OUTPUT_FILE
    )
    client.run()

import sys
import socket
import ssl
import json
import struct
import threading
import time
import os

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, 
    QLineEdit, QTextEdit, QPushButton, QLabel, QMessageBox
)
from PyQt6.QtCore import (
    QThread, pyqtSignal, QObject, QTimer, Qt
)

# --- Configuration ---
# ⚠️ Replace these values with your actual server and authentication details!
SERVER_HOST = '127.0.0.1' 
SERVER_PORT = 4443       
AUTH_TOKEN = 'your_super_secret_clone_key_12345'
SERVER_CERT_PATH = 'server.crt' # Path to the server's public certificate file

# Protocol Constants
REQUEST_METHOD = 'execute_command'
BUFFER_SIZE = 8192

# --- Network Worker Thread ---

class NetworkWorker(QObject):
    """Handles the blocking socket and SSL operations in a separate thread."""
    
    # Signals to communicate back to the main GUI thread
    connection_status = pyqtSignal(str)
    output_data = pyqtSignal(str)
    finished = pyqtSignal()
    
    def __init__(self, host, port, certfile, token):
        super().__init__()
        self.host = host
        self.port = port
        self.certfile = certfile
        self.token = token
        self.ssl_sock = None

    def receive_all(self, sock, num_bytes):
        """Helper function to guarantee reception of exactly num_bytes."""
        buffer = b''
        while len(buffer) < num_bytes:
            try:
                bytes_to_read = num_bytes - len(buffer)
                chunk = sock.recv(min(bytes_to_read, BUFFER_SIZE)) 
            except (socket.error, ssl.SSLError) as e:
                self.connection_status.emit(f"Error reading from socket: {e}")
                return None

            if not chunk:
                return None # Connection closed prematurely
            buffer += chunk
        return buffer

    def recv_response(self):
        """Receives the 4-byte length prefix and the subsequent JSON response."""
        # 1. Receive the 4-byte length prefix
        length_prefix = self.receive_all(self.ssl_sock, 4)
        if length_prefix is None:
            return None
        
        # 2. Unpack the length
        try:
            json_len = struct.unpack('!I', length_prefix)[0]
        except struct.error:
            self.connection_status.emit("Protocol error: Could not unpack length prefix.")
            return None
        
        # 3. Receive the JSON payload
        json_data = self.receive_all(self.ssl_sock, json_len)
        if json_data is None:
            return None
            
        try:
            return json.loads(json_data.decode('utf-8'))
        except json.JSONDecodeError:
            self.connection_status.emit("Protocol error: Invalid JSON response.")
            return None

    def connect_and_execute(self, command):
        """Connects, authenticates, sends command, and streams results."""
        self.connection_status.emit(f"Connecting to {self.host}:{self.port}...")
        
        # 1. Set up SSL Context
        try:
            ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=self.certfile)
            ssl_context.check_hostname = False # Trust the certificate based on CA file only
        except Exception as e:
            self.connection_status.emit(f"SSL Context Error: {e}")
            self.finished.emit()
            return
            
        # 2. Create and connect the plain socket
        plain_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            plain_sock.connect((self.host, self.port))
            self.connection_status.emit("TCP connection established. Performing SSL handshake...")
        except socket.error as e:
            self.connection_status.emit(f"TCP Connection Failed: {e}")
            plain_sock.close()
            self.finished.emit()
            return

        # 3. Wrap the socket for SSL handshake (Manual wrap as requested)
        try:
            self.ssl_sock = ssl_context.wrap_socket(plain_sock, server_hostname=self.host)
            self.connection_status.emit("SSL handshake successful. Authenticating...")
        except ssl.SSLError as e:
            self.connection_status.emit(f"SSL Handshake Failed: {e}")
            plain_sock.close()
            self.finished.emit()
            return

        # 4. Send the JSON-RPC Request
        request = {
            "method": REQUEST_METHOD,
            "token": self.token,
            "command": command
        }
        json_data = json.dumps(request).encode('utf-8')
        length_prefix = struct.pack('!I', len(json_data))
        
        try:
            self.ssl_sock.sendall(length_prefix)
            self.ssl_sock.sendall(json_data)
            self.connection_status.emit("Request sent. Awaiting server ACK...")
            
            # 5. Receive and parse the ACK/Error response
            response = self.recv_response()
            
            if response is None:
                self.connection_status.emit("Server closed connection prematurely during ACK stage.")
                return

            if response.get("error"):
                self.connection_status.emit(f"Authentication Failed: {response['error']}")
                return

            if response.get("result"):
                self.connection_status.emit(f"Server ACK: {response['result']}")
            
            # 6. Start Receiving Raw Output Stream
            self.stream_output()

        except Exception as e:
            self.connection_status.emit(f"Fatal Server/Protocol Error: {e}")
            
        finally:
            if self.ssl_sock:
                self.ssl_sock.close()
            self.connection_status.emit("Connection terminated.")
            self.finished.emit()


    def stream_output(self):
        """Continuously reads raw bytes and emits them to the GUI."""
        bytes_received = 0
        
        while True:
            try:
                data = self.ssl_sock.recv(BUFFER_SIZE)
            except Exception as e:
                self.connection_status.emit(f"Streaming Error: {e}")
                break
            
            if not data:
                # Server closed connection, stream is complete
                break
            
            # Decode data and emit to the main thread
            try:
                self.output_data.emit(data.decode('utf-8'))
            except UnicodeDecodeError:
                self.output_data.emit(f"\n[Received {len(data)} undecodable binary bytes]\n")

            bytes_received += len(data)

        self.connection_status.emit(f"Stream finished. Total bytes received: {bytes_received}")
        
# --- Main GUI Application ---

class RemoteShellApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Secure Remote Shell Client")
        self.setGeometry(100, 100, 900, 600)
        
        self.worker_thread = None
        
        if not os.path.exists(SERVER_CERT_PATH):
            QMessageBox.critical(self, "Configuration Error", 
                                 f"Server certificate file '{SERVER_CERT_PATH}' not found. Cannot proceed.")
            sys.exit(1)

        self.init_ui()
        self.update_ui_state(False)

    def init_ui(self):
        central_widget = QWidget()
        main_layout = QVBoxLayout(central_widget)
        
        # 1. Connection Status Label
        self.status_label = QLabel("Ready. Enter command below.", self)
        self.status_label.setStyleSheet("padding: 5px; background-color: #f0f0f0; border: 1px solid #ccc; font-weight: bold;")
        main_layout.addWidget(self.status_label)

        # 2. Output Console (Read-only)
        self.output_console = QTextEdit(self)
        self.output_console.setReadOnly(True)
        self.output_console.setStyleSheet("background-color: #1e1e1e; color: #00ff41; font-family: 'Consolas', 'Courier New', monospace; font-size: 10pt; border: 2px solid #555; border-radius: 5px;")
        main_layout.addWidget(self.output_console)
        
        # 3. Command Input
        self.command_input = QLineEdit(self)
        self.command_input.setPlaceholderText(f"Enter command (e.g., ls -l /):")
        self.command_input.returnPressed.connect(self.execute_command)
        self.command_input.setStyleSheet("padding: 8px; border: 2px solid #3498db; border-radius: 4px;")
        main_layout.addWidget(self.command_input)
        
        # 4. Execute Button
        self.execute_button = QPushButton("Execute Command", self)
        self.execute_button.clicked.connect(self.execute_command)
        self.execute_button.setStyleSheet("""
            QPushButton {
                background-color: #3498db; 
                color: white; 
                padding: 10px; 
                border-radius: 5px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #2980b9;
            }
            QPushButton:disabled {
                background-color: #cccccc;
            }
        """)
        main_layout.addWidget(self.execute_button)
        
        self.setCentralWidget(central_widget)

    def update_ui_state(self, is_running):
        """Toggles input/button state based on whether a command is active."""
        self.command_input.setEnabled(not is_running)
        self.execute_button.setEnabled(not is_running)
        if is_running:
            self.execute_button.setText("Executing...")
        else:
            self.execute_button.setText("Execute Command")

    def execute_command(self):
        command = self.command_input.text().strip()
        if not command:
            self.status_label.setText("Please enter a command.")
            return

        # Clear old output and set new status
        self.output_console.append(f"\n> {command}")
        self.status_label.setText("Command sent. Waiting for response...")
        self.update_ui_state(True)
        
        # 1. Initialize Worker and Thread
        self.worker = NetworkWorker(SERVER_HOST, SERVER_PORT, SERVER_CERT_PATH, AUTH_TOKEN)
        self.worker_thread = QThread()
        
        # 2. Move worker to the thread
        self.worker.moveToThread(self.worker_thread)
        
        # 3. Connect signals
        self.worker.connection_status.connect(self.update_status)
        self.worker.output_data.connect(self.append_output)
        self.worker.finished.connect(self.on_worker_finished)
        
        # 4. Start the worker when the thread starts
        self.worker_thread.started.connect(lambda: self.worker.connect_and_execute(command))
        
        # 5. Start the thread
        self.worker_thread.start()

    def update_status(self, message):
        """Updates the status bar label in the main thread."""
        self.status_label.setText(message)

    def append_output(self, data):
        """Appends streaming output data to the console in the main thread."""
        # Use insertPlainText for potentially large data chunks for better performance
        self.output_console.insertPlainText(data)
        # Ensure the scroll bar follows the new content
        self.output_console.ensureCursorVisible()

    def on_worker_finished(self):
        """Clean up when the worker thread finishes."""
        self.worker_thread.quit()
        self.worker_thread.wait()
        self.update_ui_state(False)
        self.status_label.setText("Ready. Command execution complete.")


if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = RemoteShellApp()
    window.show()
    sys.exit(app.exec())

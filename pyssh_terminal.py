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
    QLineEdit, QTextEdit, QPushButton, QLabel, QComboBox, QMessageBox, QHBoxLayout
)
from PyQt6.QtCore import (
    QThread, pyqtSignal, QObject, Qt, QTimer
)

# --- Configuration ---
# ⚠️ Replace these values with your actual server and authentication details!
SERVER_HOST = '127.0.0.1' 
SERVER_PORT = 4443       
AUTH_TOKEN = 'your_super_secret_clone_key_12345'
SERVER_CERT_PATH = 'server.crt' # Path to the server's public certificate file

# Protocol Constants
METHOD_SHELL = 'execute_command'
METHOD_CLONE = 'clone_disk'
BUFFER_SIZE = 8192

# --- Network Worker Thread ---

class NetworkWorker(QObject):
    """Handles the blocking socket and SSL operations in a separate thread."""
    
    # Signals to communicate back to the main GUI thread
    connection_status = pyqtSignal(str)
    output_text = pyqtSignal(str) # For shell output (text)
    output_binary_progress = pyqtSignal(int) # For clone progress (bytes)
    finished = pyqtSignal()
    
    def __init__(self, host, port, certfile, token, mode):
        super().__init__()
        self.host = host
        self.port = port
        self.certfile = certfile
        self.token = token
        self.mode = mode
        self.ssl_sock = None

    def receive_all(self, sock, num_bytes):
        """Helper function to guarantee reception of exactly num_bytes."""
        buffer = b''
        while len(buffer) < num_bytes:
            try:
                bytes_to_read = num_bytes - len(buffer)
                chunk = sock.recv(min(bytes_to_read, BUFFER_SIZE)) 
            except (socket.error, ssl.SSLError):
                self.connection_status.emit("Error reading from socket. Connection lost.")
                return None

            if not chunk:
                return None # Connection closed prematurely
            buffer += chunk
        return buffer

    def recv_response(self):
        """Receives the 4-byte length prefix and the subsequent JSON response."""
        length_prefix = self.receive_all(self.ssl_sock, 4)
        if length_prefix is None: return None
        
        try:
            json_len = struct.unpack('!I', length_prefix)[0]
        except struct.error:
            self.connection_status.emit("Protocol error: Could not unpack length prefix.")
            return None
        
        json_data = self.receive_all(self.ssl_sock, json_len)
        if json_data is None: return None
            
        try:
            return json.loads(json_data.decode('utf-8'))
        except json.JSONDecodeError:
            self.connection_status.emit("Protocol error: Invalid JSON response.")
            return None

    def connect_and_execute(self, argument):
        """Connects, authenticates, sends command/clone request, and streams results."""
        self.connection_status.emit(f"Connecting to {self.host}:{self.port}...")
        
        # 1. Set up SSL Context
        try:
            ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=self.certfile)
            ssl_context.check_hostname = False
        except Exception as e:
            self.connection_status.emit(f"SSL Context Error: {e}")
            self.finished.emit()
            return
            
        # 2. Create and connect the plain socket (Manual wrap as requested)
        plain_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            plain_sock.connect((self.host, self.port))
        except socket.error as e:
            self.connection_status.emit(f"TCP Connection Failed: {e}")
            plain_sock.close()
            self.finished.emit()
            return

        # 3. Wrap the socket for SSL handshake
        try:
            self.ssl_sock = ssl_context.wrap_socket(plain_sock, server_hostname=self.host)
            self.connection_status.emit("SSL handshake successful. Authenticating...")
        except ssl.SSLError as e:
            self.connection_status.emit(f"SSL Handshake Failed: {e}")
            plain_sock.close()
            self.finished.emit()
            return

        # 4. Construct and Send the JSON Request
        request = {
            "method": METHOD_SHELL if self.mode == 'shell' else METHOD_CLONE,
            "token": self.token,
        }
        if self.mode == 'shell':
            request['command'] = argument
        # Note: 'clone_disk' method doesn't require an extra payload field
        
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

            self.connection_status.emit(f"Server ACK: {response.get('result', 'Success')}. Starting stream...")
            
            # 6. Start Receiving Stream based on mode
            if self.mode == 'shell':
                self._stream_text_output()
            else:
                self._stream_binary_output(argument)

        except Exception as e:
            self.connection_status.emit(f"Fatal Server/Protocol Error: {e}")
            
        finally:
            if self.ssl_sock:
                self.ssl_sock.close()
            self.connection_status.emit("Connection terminated.")
            self.finished.emit()

    def _stream_text_output(self):
        """Reads and streams text output (for shell mode)."""
        bytes_received = 0
        while True:
            try:
                data = self.ssl_sock.recv(BUFFER_SIZE)
            except Exception:
                break
            
            if not data:
                break
            
            try:
                self.output_text.emit(data.decode('utf-8', errors='replace'))
            except Exception as e:
                self.output_text.emit(f"\n[Error decoding text: {e}]\n")

            bytes_received += len(data)
        self.connection_status.emit(f"Stream finished. Total text bytes received: {bytes_received}")

    def _stream_binary_output(self, file_path):
        """Reads raw binary data and writes it to a local file (for clone mode)."""
        bytes_received = 0
        start_time = time.time()
        
        try:
            with open(file_path, 'wb') as f:
                self.connection_status.emit(f"Writing binary stream to '{file_path}'...")
                
                while True:
                    data = self.ssl_sock.recv(BUFFER_SIZE)
                    if not data:
                        break
                    
                    f.write(data)
                    bytes_received += len(data)
                    
                    # Update progress every 1 MB
                    if bytes_received % (1024 * 1024) == 0:
                        elapsed = time.time() - start_time
                        speed = (bytes_received / (1024*1024)) / elapsed if elapsed > 0 else 0
                        
                        # Emit a complex message to update the main thread's status bar
                        status_msg = (f"CLONING... Received: {bytes_received / (1024*1024*1024):.2f} GB "
                                      f"| Speed: {speed:.2f} MB/s | Writing to: {file_path}")
                        self.output_binary_progress.emit(bytes_received)
                        self.connection_status.emit(status_msg)

            self.connection_status.emit(f"Clone finished. Total size: {bytes_received / (1024*1024*1024):.2f} GB written to '{file_path}'")
            
        except Exception as e:
            self.connection_status.emit(f"‼️ Error during file writing or data reception: {e}")
            
# --- Main GUI Application ---

class RemoteClientApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Custom Secure Protocol Client")
        self.setGeometry(100, 100, 900, 600)
        
        self.worker_thread = None
        self.current_mode = 'shell' # Default mode
        
        if not os.path.exists(SERVER_CERT_PATH):
            QMessageBox.critical(self, "Configuration Error", 
                                 f"Server certificate file '{SERVER_CERT_PATH}' not found. Cannot proceed.")
            sys.exit(1)

        self.init_ui()
        self.update_ui_state(False)
        self.update_mode_ui()

    def init_ui(self):
        central_widget = QWidget()
        main_layout = QVBoxLayout(central_widget)
        
        # Mode Selector and Status
        top_bar_layout = QHBoxLayout()
        
        self.mode_selector = QComboBox(self)
        self.mode_selector.addItem("Shell Mode (Execute Command)")
        self.mode_selector.addItem("Clone Mode (Binary Disk Stream)")
        self.mode_selector.currentIndexChanged.connect(self.mode_changed)
        top_bar_layout.addWidget(QLabel("Operation Mode:"))
        top_bar_layout.addWidget(self.mode_selector)
        
        self.status_label = QLabel("Ready. Select mode and enter input.", self)
        self.status_label.setStyleSheet("padding: 5px; background-color: #f0f0f0; border: 1px solid #ccc; font-weight: bold;")
        top_bar_layout.addWidget(self.status_label, 1) # Give status label stretch factor

        main_layout.addLayout(top_bar_layout)

        # Output Console (Read-only)
        self.output_console = QTextEdit(self)
        self.output_console.setReadOnly(True)
        self.output_console.setStyleSheet("background-color: #1e1e1e; color: #00ff41; font-family: 'Consolas', 'Courier New', monospace; font-size: 10pt; border: 2px solid #555; border-radius: 5px;")
        main_layout.addWidget(self.output_console)
        
        # Input Field
        self.command_input = QLineEdit(self)
        self.command_input.returnPressed.connect(self.execute_action)
        self.command_input.setStyleSheet("padding: 8px; border: 2px solid #3498db; border-radius: 4px;")
        main_layout.addWidget(self.command_input)
        
        # Execute Button
        self.execute_button = QPushButton("Execute Action", self)
        self.execute_button.clicked.connect(self.execute_action)
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

    def mode_changed(self, index):
        """Updates the client state and UI when the mode selector changes."""
        self.current_mode = 'shell' if index == 0 else 'clone'
        self.update_mode_ui()
        self.output_console.clear()
        self.status_label.setText("Ready.")

    def update_mode_ui(self):
        """Updates placeholder text and console visibility based on mode."""
        if self.current_mode == 'shell':
            self.command_input.setPlaceholderText("Enter command (e.g., ls -l /):")
            self.execute_button.setText("Execute Command")
            self.output_console.setVisible(True)
        else: # Clone Mode
            self.command_input.setPlaceholderText("Enter local path to save disk image (e.g., /home/user/disk.img):")
            self.execute_button.setText("Start Cloning")
            self.output_console.setVisible(True)

    def update_ui_state(self, is_running):
        """Toggles input/button/selector state based on whether an operation is active."""
        self.command_input.setEnabled(not is_running)
        self.execute_button.setEnabled(not is_running)
        self.mode_selector.setEnabled(not is_running)
        if is_running:
            self.execute_button.setText("Operation in Progress...")
        else:
            self.update_mode_ui()

    def execute_action(self):
        argument = self.command_input.text().strip()
        if not argument:
            self.status_label.setText("Please enter input (command or file path).")
            return

        self.update_ui_state(True)
        
        if self.current_mode == 'shell':
            self.output_console.append(f"\n> {argument}")
            
        # 1. Initialize Worker and Thread
        self.worker = NetworkWorker(SERVER_HOST, SERVER_PORT, SERVER_CERT_PATH, AUTH_TOKEN, self.current_mode)
        self.worker_thread = QThread()
        
        # 2. Move worker to the thread
        self.worker.moveToThread(self.worker_thread)
        
        # 3. Connect signals
        self.worker.connection_status.connect(self.update_status)
        self.worker.output_text.connect(self.append_output) # Shell output
        self.worker.output_binary_progress.connect(self.update_clone_progress) # Clone progress
        self.worker.finished.connect(self.on_worker_finished)
        
        # 4. Start the worker when the thread starts
        self.worker_thread.started.connect(lambda: self.worker.connect_and_execute(argument))
        
        # 5. Start the thread
        self.worker_thread.start()

    def update_status(self, message):
        """Updates the status bar label in the main thread."""
        self.status_label.setText(message)

    def append_output(self, data):
        """Appends streaming shell output data to the console in the main thread."""
        self.output_console.insertPlainText(data)
        self.output_console.ensureCursorVisible()

    def update_clone_progress(self, bytes_received):
        """Handles binary progress updates (currently status bar shows full progress)."""
        # This signal is mainly here for future, more complex UI like a progress bar.
        # For now, the status bar update (handled by connection_status) is sufficient.
        pass

    def on_worker_finished(self):
        """Clean up when the worker thread finishes."""
        self.worker_thread.quit()
        self.worker_thread.wait()
        self.update_ui_state(False)


if __name__ == '__main__':
    # Use Fusion style for a better look
    try:
        QApplication.setStyle('Fusion')
    except:
        pass # Fallback if style is unavailable
        
    app = QApplication(sys.argv)
    window = RemoteClientApp()
    window.show()
    sys.exit(app.exec())

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
import threading
import time

class IBKRApp(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.connected = False
        
    def connectAck(self):
        """Callback when connection acknowledged"""
        self.connected = True
        print("✓ Connection established")
        
    def connectionClosed(self):
        """Callback when connection closed"""
        self.connected = False
        print("✗ Connection closed")

# Initialize app
app = IBKRApp()

# Connect
print("Connecting to IBKR...")
app.connect("127.0.0.1", 4002, 1)  # host, port, client_id

# Start message processing thread
thread = threading.Thread(target=app.run, daemon=True)
thread.start()

# Wait for connection
time.sleep(2)

# Check connection status (inline)
if app.isConnected():
    print("✓ IBKR is CONNECTED")
else:
    print("✗ IBKR is NOT connected")

# Alternative: Check internal flag
if app.connected:
    print("✓ Connection callback received")
    
# Disconnect
app.disconnect()

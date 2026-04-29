import serial
import serial.tools.list_ports
import threading


class MCUProtocol:
    """Serial communication backend for the STM32 NUCLEO actuator controller.

    Thread-safe: connect/disconnect may be called from the main thread while
    the internal read loop runs in a daemon thread.  All registered callbacks
    are invoked from the read thread – callers must marshal UI updates to the
    main thread themselves (e.g. widget.after(0, ...)).
    """

    BAUD = 115200

    def __init__(self):
        self._serial: serial.Serial | None = None
        self._buf = ""
        self._callbacks: list = []
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def list_ports() -> list[str]:
        return sorted(p.device for p in serial.tools.list_ports.comports())

    @property
    def connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def connect(self, port: str, baud: int = BAUD) -> None:
        """Open the serial port and start the background read thread."""
        if self.connected:
            self.disconnect()
        ser = serial.Serial(port, baud, timeout=0.1)
        with self._lock:
            self._serial = ser
            self._buf = ""
            self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True, name="mcu-rx")
        self._thread.start()

    def disconnect(self) -> None:
        """Stop the read thread and close the port."""
        with self._lock:
            self._running = False
            ser = self._serial
            self._serial = None
            
        if ser and ser.is_open:
            try:
                ser.close()
            except Exception:
                pass
                
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
            self._thread = None

    def send(self, cmd: str) -> None:
        """Send an ASCII command terminated with \\n."""
        with self._lock:
            ser = self._serial
            if ser and ser.is_open:
                try:
                    import time
                    for char in (cmd + "\n"):
                        ser.write(char.encode("ascii"))
                        ser.flush()
                        time.sleep(0.002) # 2ms per character gives MCU ample time to process RX interrupts
                except Exception:
                    pass

    def add_callback(self, fn) -> None:
        """Register a callable that receives each complete line (str)."""
        if fn not in self._callbacks:
            self._callbacks.append(fn)

    def remove_callback(self, fn) -> None:
        try:
            self._callbacks.remove(fn)
        except ValueError:
            pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _read_loop(self) -> None:
        while True:
            with self._lock:
                if not self._running:
                    break
                ser = self._serial
            if ser is None or not ser.is_open:
                break
            try:
                waiting = ser.in_waiting
                data = ser.read(waiting if waiting > 0 else 1)
                if data:
                    with self._lock:
                        self._buf += data.decode("utf-8", errors="replace")
                    self._flush_lines()
            except Exception:
                break

    def _flush_lines(self) -> None:
        while True:
            with self._lock:
                # Find the earliest line terminator
                cr = self._buf.find("\r")
                nl = self._buf.find("\n")
                if cr < 0 and nl < 0:
                    break
                if cr < 0:
                    pos, skip = nl, 1
                elif nl < 0:
                    pos, skip = cr, 1
                else:
                    pos = min(cr, nl)
                    # Handle \r\n as a single terminator
                    skip = 2 if self._buf[pos:pos + 2] in ("\r\n", "\n\r") else 1

                line = self._buf[:pos].strip()
                self._buf = self._buf[pos + skip:]
                
            if line:
                for cb in list(self._callbacks):
                    try:
                        cb(line)
                    except Exception:
                        pass

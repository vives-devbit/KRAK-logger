import requests
from.import utility as utility
import numpy as np
import socket
from openapi.openapi_header import *
from openapi.openapi_stream import *

class LanXI:
    def __init__(self, ip):
        self.ip = ip
        if self.ip is None:
            self.host = None
            self.sample_rate = 25600  # Default sample rate for testing
            print("Warning: No LAN-XI IP address configured. Running in offline mode.")
        else:
            self.host = "http://" + self.ip
            print(f"LAN-XI configured with IP: {self.ip}")
        
    def setup_stream(self):
        """
        Setup of channel 1 with a microphone
        """
        if self.host is None:
            print("Warning: Cannot setup stream - no LAN-XI device configured")
            return
        # This setup is indentical to the one found in "Streaming.py", refer to this for more info.
        # First, try to clean up any existing state
        import time
        print("Cleaning up any previous LAN-XI state...")
        try:
            requests.put(self.host + "/rest/rec/measurements/stop", timeout=2)
            time.sleep(0.5)
            requests.put(self.host + "/rest/rec/finish", timeout=2)
            time.sleep(0.5)
            requests.put(self.host + "/rest/rec/close", timeout=2)
            time.sleep(1)  # Give device time to fully close
            print("Previous state cleaned up")
        except Exception as e:
            print(f"Cleanup note: {e}")  # Log but continue
        
        # Open recorder application
        print("Opening recorder application...")
        requests.put(self.host + "/rest/rec/open")
        # Get information about the device and configure 
        self.GetTeds()
        self.ConfigureStream()
        self.GetFs()


    def GetTeds(self):
        # Start TEDS detection, we then check when it is done and read it out as JSON
        # Detect TEDS
        self.response = requests.post(self.host + "/rest/rec/channels/input/all/transducers/detect")
        while requests.get(self.host + "/rest/rec/onchange").json()["transducerDetectionActive"]:
            pass
        # Get TEDS information
        self.response = requests.get(self.host + "/rest/rec/channels/input/all/transducers")
        self.channels = self.response.json()


    def ConfigureStream(self):
        # To start a stream we first need to set a configuration. In this example we create a configuration by requesting a default channel setup. We use a tiny utility function to update all values with a given key.
        # Create a new recording
        self.response = requests.put(self.host + "/rest/rec/create")
        # Get Default setup for channels
        self.response = requests.get(self.host + "/rest/rec/channels/input/default")
        self.setup = self.response.json()
        # Replace stream destination from default SD card to socket
        utility.update_value("destinations", ["socket"], self.setup)
        # Set enabled to false for all channels
        utility.update_value("enabled", False, self.setup)
        # Enable channels with valid TEDS
        for channel_nr in range(len(self.channels)):
            if self.channels[channel_nr] != None:
                self.setup["channels"][channel_nr]["transducer"] = self.channels[channel_nr]
                self.setup["channels"][channel_nr]["enabled"] = True
                self.setup["channels"][channel_nr]["ccld"] = self.channels[channel_nr]["requiresCcld"]
        # Configure channel 2 (index 1) as analog force channel with 10 Vpeak range
        # Only override if no TEDS detected on channel 2
        if len(self.setup["channels"]) > 1 and self.channels[1] == None:
            self.setup["channels"][1]["enabled"] = True
            self.setup["channels"][1]["ccld"] = False
            self.setup["channels"][1]["range"] = "10 Vpeak"  # Set the correct range for force sensor
            self.setup["channels"][1]["filter"] = "DC" # Set filter to DC for force sensor
        # Remove None channels
        # self.channels = list(filter(lambda x : x != None, self.channels))
        # remove disabled channels
        self.channels = list(filter(lambda x : x["enabled"] == True, self.setup["channels"]))
        print(self.setup)
        if not any(self.channels):
            print("No channels enabled! Did you connect a microphone?")
            exit()
        # Next we setup the input channels for streaming. We use the input setup we got previosly.
        # Create input channels with the setup
        try:
            self.response = requests.put(self.host + "/rest/rec/channels/input", json = self.setup)
            if self.response.status_code != 200:
                raise Exception(f"Failed to configure channels: {self.response.status_code} - {self.response.text}")
            # Get streaming socket
            self.response = requests.get(self.host + "/rest/rec/destination/socket")
            socket_info = self.response.json()
            self.inputport = socket_info["tcpPort"]
            print(f"Stream configured successfully. Socket port: {self.inputport}")
        except Exception as e:
            print(f"Error configuring stream: {e}")
            # Clean up the failed recording
            try:
                requests.put(self.host + "/rest/rec/finish", timeout=2)
                requests.put(self.host + "/rest/rec/close", timeout=2)
            except:
                pass
            raise e


    def GetFs(self):
        # Sample rate is found by doubling the channel bandwidth and finding the closest supported sample rate
        # Channel bandwidth is found in the channel setup, it is in string format, so to get it as a number replace khz with *1000 and evaluate
        bandwidth = self.setup["channels"][0]["bandwidth"]
        bandwidth = bandwidth.replace('kHz', '*1000')
        bandwidth = eval(bandwidth)
        self.response = requests.get(self.host + "/rest/rec/module/info")
        module_info = self.response.json()
        supported_sample_rates = module_info["supportedSampleRates"]
        # Find the sample rate with the minimum difference to bandwidth * 2
        self.sample_rate = min(supported_sample_rates, key = lambda x:abs(x - bandwidth * 2))

    def reset_stream(self):
        """
        Reset the streaming configuration if connection issues occur.
        """
        if self.host is None:
            return
        import time
        try:
            print("Resetting stream...")
            # Stop any ongoing measurement
            requests.put(self.host + "/rest/rec/measurements/stop", timeout=2)
            time.sleep(0.5)
            # Finish and close
            requests.put(self.host + "/rest/rec/finish", timeout=2)
            time.sleep(0.5)
            requests.put(self.host + "/rest/rec/close", timeout=2)
            time.sleep(1)
            # Reopen and reconfigure
            requests.put(self.host + "/rest/rec/open", timeout=2)
            self.ConfigureStream()
            print("Stream reset complete")
        except Exception as e:
            print(f"Warning during stream reset: {e}")

    def SampleChannels(self, duration):
        """
        Sample all three channels for the given duration (in seconds).
        Returns:
            time_axis: np.ndarray of time values
            data: np.ndarray shape (3, N) where N is the number of samples
        """
        if self.host is None:
            raise RuntimeError("No LAN-XI device configured. Cannot sample channels without hardware.")
        sample_rate = self.sample_rate
        num_samples = int(sample_rate * duration)
        arrays = [[], [], []]  # For channel 1, 2, and 3
        interpretations = [{},{},{},{},{},{}]

        import requests
        try:
            self.response = requests.post(self.host + "/rest/rec/measurements")
        except Exception as e:
            print(f"Failed to start measurement, attempting to reset stream: {e}")
            self.reset_stream()
            self.response = requests.post(self.host + "/rest/rec/measurements")
        
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                print(f"Attempting to connect to {self.ip}:{self.inputport}")
                s.connect((self.ip, self.inputport))
                print(f"Successfully connected to streaming socket")
                total_samples = 0
                while total_samples <= num_samples:
                    # Get header
                    data = s.recv(28)
                    wstream = OpenapiHeader.from_bytes(data)
                    content_length = wstream.content_length + 28
                    # Get rest of package
                    while len(data) < content_length:
                        packet = s.recv(content_length - len(data))
                        data += packet
                    # Parse package
                    package = OpenapiStream.from_bytes(data)
                    if package.header.message_type == OpenapiStream.Header.EMessageType.e_interpretation:
                        for interpretation in package.content.interpretations:
                            interpretations[interpretation.signal_id - 1][interpretation.descriptor_type] = interpretation.value
                    if package.header.message_type == OpenapiStream.Header.EMessageType.e_signal_data:
                        for signal in package.content.signals:
                            if signal is not None and (signal.signal_id == 1 or signal.signal_id == 2 or signal.signal_id == 3):
                                scale_factor = interpretations[signal.signal_id - 1].get(
                                    OpenapiStream.Interpretation.EDescriptorType.scale_factor, 1.0
                                )
                                values = np.array([x.calc_value for x in signal.values])
                                # Convert to volts (divide by 2^23 and multiply by scale factor)
                                values = (values * scale_factor) / (2 ** 23)
                                arrays[signal.signal_id - 1].extend(values)
                                if signal.signal_id == 1:
                                    total_samples = len(arrays[0])
                # Stop measurement
                requests.put(self.host + "/rest/rec/measurements/stop")
        except Exception as e:
            # Ensure measurement is stopped even on error
            try:
                requests.put(self.host + "/rest/rec/measurements/stop")
            except:
                pass
            raise e

        # Truncate to the same length and to num_samples
        min_len = min(len(arrays[0]), len(arrays[1]), len(arrays[2]), num_samples)
        ch1 = np.array(arrays[0][:min_len])
        ch2 = np.array(arrays[1][:min_len])
        ch3 = np.array(arrays[2][:min_len])
        time_axis = np.linspace(0, min_len / sample_rate, min_len, endpoint=False)
        data = np.vstack([ch1, ch2, ch3])
        return time_axis, data
    
    def close_stream(self):
        """
        Properly finish and close the LAN-XI recorder application.
        Call this when closing the program.
        """
        if self.host is None:
            print("No LAN-XI device to close")
            return
        requests.put(self.host + "/rest/rec/finish")
        requests.put(self.host + "/rest/rec/close")
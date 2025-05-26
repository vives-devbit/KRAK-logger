import requests
from.import utility as utility
import numpy as np
import socket
from openapi.openapi_header import *
from openapi.openapi_stream import *

class LanXI:
    def __init__(self, ip):
        self.ip = ip
        self.host = "http://" + self.ip
        
    def setup_stream(self):
        """
        Setup of channel 1 with a microphone
        """
        # This setup is indentical to the one found in "Streaming.py", refer to this for more info.
        # Open recorder application
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
        if len(self.setup["channels"]) > 1:
            self.setup["channels"][1]["enabled"] = True
            self.setup["channels"][1]["ccld"] = False
            self.setup["channels"][1]["range"] = "10 Vpeak"  # Set the correct range for force sensor
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

        self.response = requests.put(self.host + "/rest/rec/channels/input", json = self.setup)
        # Get streaming socket
        self.response = requests.get(self.host + "/rest/rec/destination/socket")
        self.inputport = self.response.json()["tcpPort"]


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

    def SampleChannels(self, duration):
        """
        Sample both channels for the given duration (in seconds).
        Returns:
            time_axis: np.ndarray of time values
            data: np.ndarray shape (2, N) where N is the number of samples
        """
        sample_rate = self.sample_rate
        num_samples = int(sample_rate * duration)
        arrays = [[], []]  # For channel 1 and 2
        interpretations = [{},{},{},{},{},{}]

        import requests
        self.response = requests.post(self.host + "/rest/rec/measurements")
        
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((self.ip, self.inputport))
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
                        if signal is not None and (signal.signal_id == 1 or signal.signal_id == 2):
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
            import requests
            requests.put(self.host + "/rest/rec/measurements/stop")
            s.close()

        # Truncate to the same length and to num_samples
        min_len = min(len(arrays[0]), len(arrays[1]), num_samples)
        ch1 = np.array(arrays[0][:min_len])
        ch2 = np.array(arrays[1][:min_len])
        time_axis = np.linspace(0, min_len / sample_rate, min_len, endpoint=False)
        data = np.vstack([ch1, ch2])
        return time_axis, data
    
    def close_stream(self):
        """
        Properly finish and close the LAN-XI recorder application.
        Call this when closing the program.
        """
        requests.put(self.host + "/rest/rec/finish")
        requests.put(self.host + "/rest/rec/close")
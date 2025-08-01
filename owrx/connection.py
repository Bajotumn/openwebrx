from owrx.details import ReceiverDetails
from owrx.dsp import DspManager
from owrx.cpu import CpuUsageThread
from owrx.sdr import SdrService
from owrx.source import SdrSourceState, SdrClientClass, SdrSourceEventClient
from owrx.client import ClientRegistry, TooManyClientsException, BannedClientException
from owrx.feature import FeatureDetector
from owrx.version import openwebrx_version
from owrx.bands import Bandplan
from owrx.bookmarks import Bookmarks
from owrx.web.repeaters import Repeaters
from owrx.web.eibi import EIBI
from owrx.map import Map
from owrx.property import PropertyStack, PropertyDeleted
from owrx.modes import Modes, DigitalMode
from owrx.config import Config
from owrx.waterfall import WaterfallOptions
from owrx.websocket import Handler
from queue import Queue, Full, Empty
from abc import ABCMeta, abstractmethod
import json
import threading
import struct
import time

import logging

logger = logging.getLogger(__name__)

PoisonPill = object()


class Client(Handler, metaclass=ABCMeta):
    def __init__(self, conn):
        self.conn = conn
        self.multithreadingQueue = Queue(100)

        def mp_passthru():
            run = True
            while run:
                try:
                    data = self.multithreadingQueue.get()
                    if data is PoisonPill:
                        run = False
                    else:
                        self.send(data)
                    self.multithreadingQueue.task_done()
                except (EOFError, OSError, ValueError):
                    run = False
                except Exception:
                    logger.exception("Exception on client multithreading queue")

            # unset the queue object to free shared memory file descriptors
            self.multithreadingQueue = None

        threading.Thread(target=mp_passthru, name="connection_mp_passthru").start()

    def send(self, data):
        try:
            self.conn.send(data)
        except IOError:
            logger.exception("error in Client::send()")
            self.close(error=True)

    def close(self, error: bool = False):
        if self.multithreadingQueue is not None:
            while True:
                try:
                    self.multithreadingQueue.get(block=False)
                except Empty:
                    break
            try:
                self.multithreadingQueue.put(PoisonPill, block=False)
            except Full:
                # this shouldn't happen, we just emptied the queue, but it's not worth risking the exception
                logger.exception("impossible queue state: Full after Empty")
        self.conn.close(socketError=error)

    def mp_send(self, data):
        if self.multithreadingQueue is None:
            return
        try:
            self.multithreadingQueue.put(data, block=False)
        except Full:
            self.close(error=True)

    @abstractmethod
    def handleTextMessage(self, conn, message):
        pass

    def handleBinaryMessage(self, conn, data):
        logger.error("unsupported binary message, discarding")

    def handleClose(self):
        self.close()


class OpenWebRxClient(Client, metaclass=ABCMeta):
    def __init__(self, conn):
        super().__init__(conn)

        receiver_details = ReceiverDetails()

        def send_receiver_info(*args):
            receiver_info = receiver_details.__dict__()
            self.write_receiver_details(receiver_info)

        self._detailsSubscription = receiver_details.wire(send_receiver_info)
        send_receiver_info()

    def write_receiver_details(self, details):
        self.send({"type": "receiver_details", "value": details})

    def close(self, error: bool = False):
        self._detailsSubscription.cancel()
        super().close(error)


class OpenWebRxReceiverClient(OpenWebRxClient, SdrSourceEventClient):
    sdr_config_keys = [
        "waterfall_levels",
        "waterfall_auto_level_default_mode",
        "samp_rate",
        "start_mod",
        "start_freq",
        "center_freq",
        "tuning_step",
        "initial_squelch_level",
        "sdr_id",
        "profile_id",
        "squelch_auto_margin",
    ]

    global_config_keys = [
        "waterfall_scheme",
        "waterfall_colors",
        "waterfall_auto_levels",
        "waterfall_auto_min_range",
        "fft_size",
        "audio_compression",
        "fft_compression",
        "max_clients",
        "tuning_precision",
        "allow_center_freq_changes",
        "allow_audio_recording",
        "allow_chat",
        "callsign_url",
        "vessel_url",
        "flight_url",
        "modes_url",
        "receiver_gps",
    ]

    def __init__(self, conn):
        super().__init__(conn)

        self.dsp = None
        self.dspLock = threading.Lock()
        self.sdr = None
        self.configSubs = []
        self.bookmarkSub = None
        self.connectionProperties = {}

        # Get initial robot score based on the number of recent connections
        self.lastProfileChange = time.time()
        self.robotAlert = ClientRegistry.getSharedInstance().robotScore(self)
        # Ban the suspected robot
        if self.robotAlert >= 30:
            ClientRegistry.getSharedInstance().banClient(self, 60 * 12)

        try:
            ClientRegistry.getSharedInstance().addClient(self)
        except TooManyClientsException:
            self.write_backoff_message("Too many clients")
            self.close()
            raise
        except BannedClientException:
            self.write_backoff_message("Client address banned")
            self.close()
            raise

        self.setupGlobalConfig()
        self.stack = self.setupStack()

        self.setSdr()

        features = FeatureDetector().feature_availability()
        self.write_features(features)

        modes = Modes.getAvailableClientModes()
        self.write_modes(modes)

        self.configSubs.append(SdrService.getActiveSources().wire(self._onSdrDeviceChanges))
        self.configSubs.append(SdrService.getAvailableProfiles().wire(self._sendProfiles))
        self._sendProfiles()

        CpuUsageThread.getSharedInstance().add_client(self)

    def setupStack(self):
        stack = PropertyStack()
        # stack layer 0 reserved for sdr properties
        # stack.addLayer(0, self.sdr.getProps())
        stack.addLayer(1, Config.get())
        configProps = stack.filter(*OpenWebRxReceiverClient.sdr_config_keys)

        def sendConfig(changes=None):
            if changes is None:
                config = configProps.__dict__()
            else:
                # transform deletions into Nones
                config = {k: v if v is not PropertyDeleted else None for k, v in changes.items()}
            if (
                (changes is None or "start_freq" in changes or "center_freq" in changes)
                and "start_freq" in configProps
                and "center_freq" in configProps
            ):
                config["start_offset_freq"] = configProps["start_freq"] - configProps["center_freq"]
            if (changes is None or "profile_id" in changes) and self.sdr is not None:
                config["sdr_id"] = self.sdr.getId()
            self.write_config(config)

        def sendBookmarks(*args):
            cf = configProps["center_freq"]
            srh = configProps["samp_rate"] / 2
            dial_frequencies = []
            bookmarks = []
            if "center_freq" in configProps and "samp_rate" in configProps:
                frequencyRange = (cf - srh, cf + srh)
                dial_frequencies = Bandplan.getSharedInstance().collectDialFrequencies(frequencyRange)
                bookmarks = [b.__dict__() for b in Bookmarks.getSharedInstance().getBookmarks(frequencyRange)]
                # Search EIBI schedule for bookmarks, if enabled
                range = self.stack["eibi_bookmarks_range"]
                if range > 0:
                    bookmarks += [b.__dict__() for b in EIBI.getSharedInstance().currentBookmarks(frequencyRange, rangeKm=range)]
                # Search RepeaterBook for bookmarks, if enabled
                range = self.stack["repeater_range"]
                if range > 0:
                    bookmarks += [b.__dict__() for b in Repeaters.getSharedInstance().getBookmarks(frequencyRange, rangeKm=range)]
            self.write_dial_frequencies(dial_frequencies)
            self.write_bookmarks(bookmarks)

        def sendBands(*args):
            if "center_freq" in configProps and "samp_rate" in configProps:
                cf = configProps["center_freq"]
                srh = configProps["samp_rate"] / 2
                bands = Bandplan.getSharedInstance().findBandsInRange(cf - srh, cf + srh)
                self.write_bands([{
                    "name"       : x.getName(),
                    "low_bound"  : x.getBounds()[0],
                    "high_bound" : x.getBounds()[1],
                    "tags"       : x.getTags()
                } for x in bands])

        def updateBookmarkSubscription(*args):
            if self.bookmarkSub is not None:
                self.bookmarkSub.cancel()
                self.bookmarkSub = None
            if "center_freq" in configProps and "samp_rate" in configProps:
                cf = configProps["center_freq"]
                srh = configProps["samp_rate"] / 2
                frequencyRange = (cf - srh, cf + srh)
                self.bookmarkSub = Bookmarks.getSharedInstance().subscribe(frequencyRange, sendBookmarks)
                sendBookmarks()

        self.configSubs.append(configProps.wire(sendConfig))
        self.configSubs.append(stack.filter("center_freq", "samp_rate").wire(updateBookmarkSubscription))
        self.configSubs.append(stack.filter("center_freq", "samp_rate").wire(sendBands))

        # send initial config
        sendConfig()
        return stack

    def setupGlobalConfig(self):
        def writeConfig(changes):
            # TODO it would be nicer to have all options available and switchable in the client
            # this restores the existing functionality for now, but there is lots of potential
            if "waterfall_scheme" in changes or "waterfall_colors" in changes:
                scheme = WaterfallOptions(globalConfig["waterfall_scheme"]).instantiate()
                changes["waterfall_colors"] = scheme.getColors()
            self.write_config(changes)

        globalConfig = Config.get().filter(*OpenWebRxReceiverClient.global_config_keys)
        self.configSubs.append(globalConfig.wire(writeConfig))
        writeConfig(globalConfig.__dict__())

    def onStateChange(self, state: SdrSourceState):
        if state is SdrSourceState.RUNNING:
            self.handleSdrAvailable()

    def onFail(self):
        logger.warning('SDR device "%s" has failed, selecting new device', self.sdr.getName())
        self.write_log_message('SDR device "{0}" has failed, selecting new device'.format(self.sdr.getName()))
        self.setSdr()

    def onDisable(self):
        logger.warning('SDR device "%s" was disabled, selecting new device', self.sdr.getName())
        self.write_log_message('SDR device "{0}" was disabled, selecting new device'.format(self.sdr.getName()))
        self.setSdr()

    def onShutdown(self):
        logger.warning('SDR device "%s" is shutting down, selecting new device', self.sdr.getName())
        self.write_log_message('SDR device "{0}" is shutting down, selecting new device'.format(self.sdr.getName()))
        self.setSdr()

    def getClientClass(self) -> SdrClientClass:
        return SdrClientClass.USER

    def _onSdrDeviceChanges(self, changes):
        # restart the client if an sdr has become available
        if self.sdr is None and any(s is not PropertyDeleted for s in changes.values()):
            self.setSdr()

    def _sendProfiles(self, *args):
        profiles = [{"id": pid, "name": name} for pid, name in SdrService.getAvailableProfileNames().items()]
        self.write_profiles(profiles)

    def handleTextMessage(self, conn, message):
        try:
            message = json.loads(message)
            if "type" in message:
                if message["type"] == "dspcontrol":
                    dsp = self.getDsp()
                    if dsp is None:
                        logger.warning("DSP not available; discarding client dspcontrol message")
                    else:
                        if "action" in message and message["action"] == "start":
                            dsp.start()

                        if "params" in message:
                            params = message["params"]
                            dsp.setProperties(params)

                elif message["type"] == "setsdr":
                    if "params" in message and "sdr" in message["params"]:
                        self.setSdr(message["params"]["sdr"])
                elif message["type"] == "selectprofile":
                    if "params" in message and "profile" in message["params"]:
                        params  = message["params"]
                        profile = params["profile"].split("|")
                        key     = params["key"] if "key" in params else None
                        self.setProfile(profile[0], profile[1], key)
                elif message["type"] == "setfrequency":
                    # If the magic key is set in the settings, only allow
                    # changes if it matches the received key
                    if "params" in message and "frequency" in message["params"]:
                        if self.stack["allow_center_freq_changes"]:
                            params = message["params"]
                            magic  = self.stack["magic_key"]
                            key    = params["key"] if "key" in params else None
                            if magic == "" or key == magic:
                                self.sdr.setCenterFreq(params["frequency"])
                elif message["type"] == "connectionproperties":
                    if "params" in message:
                        self.connectionProperties = message["params"]
                        if self.dsp:
                            self.getDsp().setProperties(self.connectionProperties)
                elif message["type"] == "sendmessage":
                    if "text" in message:
                        ClientRegistry.getSharedInstance().broadcastChatMessage(
                            self,
                            message["text"],
                            message["name"] if "name" in message else None
                        )

            else:
                logger.warning("received message without type: {0}".format(message))

        except json.JSONDecodeError:
            logger.warning("message is not json: {0}".format(message))

    def setProfile(self, sdr: str, profile: str, key: str = None):
        # Set new SDR source
        self.setSdr(sdr)

        # Locked source's profile can only be changed with a key
        magic = self.stack["magic_key"]
        if self.sdr.isLocked() and magic != "" and key != magic:
            # Force update back to the current profile
            self.resetSdr()

        else:
            # Keep track of frequent profile changes
            thisChange = time.time()
            robotScore = 10 - (thisChange - self.lastProfileChange)
            self.lastProfileChange = thisChange;

            # Keep the robot score
            if robotScore < 0:
                self.robotAlert = 0
            else:
                self.robotAlert += robotScore

            # If this is not a robot...
            if self.robotAlert < 30:
                # Select a new profile
                self.sdr.activateProfile(profile)
            else:
                # Ban the suspected robot
                ClientRegistry.getSharedInstance().banClient(self, 60 * 12)

    def setSdr(self, id=None):
        next = None
        if id is not None:
            next = SdrService.getSource(id)
        if next is None:
            next = SdrService.getFirstSource()

        # exit condition: no change
        if next == self.sdr and next is not None:
            return

        self.stopDsp()
        self.stack.removeLayerByPriority(0)

        if self.sdr is not None:
            self.sdr.removeClient(self)

        self.sdr = next

        if next is None:
            # exit condition: no sdrs available
            logger.warning("no more SDR devices available")
            self.handleNoSdrsAvailable()
            return

        self.sdr.addClient(self)

    def resetSdr(self):
        if self.sdr is not None:
            self.stopDsp()
            self.stack.removeLayerByPriority(0)
            self.sdr.removeClient(self)
            self.sdr.addClient(self)

    def handleSdrAvailable(self):
        self.getDsp().setProperties(self.connectionProperties)
        self.stack.replaceLayer(0, self.sdr.getProps())

        self.sdr.addSpectrumClient(self)

    def handleNoSdrsAvailable(self):
        self.write_sdr_error("No SDR Devices available")

    def close(self, error: bool = False):
        if self.sdr is not None:
            self.sdr.removeClient(self)
        self.stopDsp()
        CpuUsageThread.getSharedInstance().remove_client(self)
        ClientRegistry.getSharedInstance().removeClient(self)
        while self.configSubs:
            self.configSubs.pop().cancel()
        if self.bookmarkSub is not None:
            self.bookmarkSub.cancel()
            self.bookmarkSub = None
        super().close(error)

    def stopDsp(self):
        with self.dspLock:
            if self.dsp is not None:
                self.dsp.stop()
                self.dsp = None
        if self.sdr is not None:
            self.sdr.removeSpectrumClient(self)

    def getDsp(self):
        with self.dspLock:
            if self.dsp is None and self.sdr is not None:
                self.dsp = DspManager(self, self.sdr)
        return self.dsp

    def write_spectrum_data(self, data):
        self.mp_send(bytes([0x01]) + data)

    def write_dsp_data(self, data):
        self.send(bytes([0x02]) + data)

    def write_hd_audio(self, data):
        self.send(bytes([0x04]) + data)

    def write_s_meter_level(self, level):
        # may contain more than one sample, so only take the last 4 bytes = 1 float
        level, = struct.unpack('f', level[-4:])
        try:
            self.send({"type": "smeter", "value": level})
        except ValueError:
            logger.warning("unable to send smeter value: %s", str(level))

    def write_cpu_usage(self, usage):
        self.mp_send({"type": "cpuusage", "value": usage})

    def write_temperature(self, temp):
        self.mp_send({"type": "temperature", "value": temp})

    def write_clients(self, clients):
        self.mp_send({"type": "clients", "value": clients})

    def write_secondary_fft(self, data):
        self.send(bytes([0x03]) + data)

    def write_secondary_demod(self, message):
        self.send({"type": "secondary_demod", "value": message})

    def write_secondary_dsp_config(self, cfg):
        self.send({"type": "secondary_config", "value": cfg})

    def write_config(self, cfg):
        self.send({"type": "config", "value": cfg})

    def write_profiles(self, profiles):
        self.send({"type": "profiles", "value": profiles})

    def write_features(self, features):
        self.send({"type": "features", "value": features})

    def write_metadata(self, metadata):
        self.send({"type": "metadata", "value": metadata})

    def write_dial_frequencies(self, frequencies):
        self.send({"type": "dial_frequencies", "value": frequencies})

    def write_bookmarks(self, bookmarks):
        self.send({"type": "bookmarks", "value": bookmarks})

    def write_bands(self, bands):
        self.send({"type": "bands", "value": bands})

    def write_log_message(self, message):
        self.send({"type": "log_message", "value": message})

    def write_sdr_error(self, message):
        self.send({"type": "sdr_error", "value": message})

    def write_demodulator_error(self, message):
        self.send({"type": "demodulator_error", "value": message})

    def write_backoff_message(self, reason):
        self.send({"type": "backoff", "reason": reason})

    def write_chat_message(self, name, text, color = "white"):
        self.send({
            "type": "chat_message",
            "name": name,
            "text": text,
            "color": color
        })

    def write_modes(self, modes):
        def to_json(m):
            res = {
                "modulation": m.modulation,
                "name": m.name,
                "type": "digimode" if isinstance(m, DigitalMode) else "analog",
                "requirements": m.requirements,
                "squelch": m.squelch,
            }
            if m.bandpass is not None:
                res["bandpass"] = {"low_cut": m.bandpass.low_cut, "high_cut": m.bandpass.high_cut}
            if m.ifRate is not None:
                res["ifRate"] = m.ifRate
            if isinstance(m, DigitalMode):
                res["underlying"] = m.underlying
                res["secondaryFft"] = m.secondaryFft
            return res

        self.send({"type": "modes", "value": [to_json(m) for m in modes]})


class MapConnection(OpenWebRxClient):
    def __init__(self, conn):
        super().__init__(conn)

        pm = Config.get()
        filtered_config = pm.filter(
            "google_maps_api_key",
            "openweathermap_api_key",
            "receiver_gps",
            "map_type",
            "map_position_retention_time",
            "map_ignore_indirect_reports",
            "map_prefer_recent_reports",
            "map_call_retention_time",
            "map_max_calls",
            "callsign_url",
            "vessel_url",
            "flight_url",
            "modes_url",
            "receiver_name",
        )
        self.configSub = filtered_config.wire(self.write_config)

        self.write_config(filtered_config.__dict__())

        Map.getSharedInstance().addClient(self)

    def handleTextMessage(self, conn, message):
        pass

    def close(self, error: bool = False):
        Map.getSharedInstance().removeClient(self)
        self.configSub.cancel()
        super().close(error)

    def write_config(self, cfg):
        self.send({"type": "config", "value": cfg})

    def write_update(self, update):
        self.mp_send({"type": "update", "value": update})


class HandshakeMessageHandler(Handler):
    """
    This handler receives text messages, but will only respond to the second handshake string.
    As soon as a valid handshake is received, the handler replaces itself with the corresponding handler type.
    """
    def handleTextMessage(self, conn, message):
        if message[:16] == "SERVER DE CLIENT":
            meta = message[17:].split(" ")
            handshake = {v[0]: "=".join(v[1:]) for v in map(lambda x: x.split("="), meta)}

            logger.debug("client connection initialized")

            client = None
            if "type" in handshake:
                if handshake["type"] == "receiver":
                    client = OpenWebRxReceiverClient
                elif handshake["type"] == "map":
                    client = MapConnection
                else:
                    logger.warning("invalid connection type: %s", handshake["type"])

            if client is not None:
                logger.debug("handshake complete, handing off to %s", client.__name__)
                # hand off all further communication to the correspondig connection
                conn.send("CLIENT DE SERVER server=openwebrx version={version}".format(version=openwebrx_version))
                conn.setMessageHandler(client(conn))
            else:
                logger.warning('invalid handshake received')
        else:
            logger.warning("not answering client request since handshake is not complete")

    def handleBinaryMessage(self, conn, data):
        pass

    def handleClose(self):
        pass

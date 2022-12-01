""" Reolink NVR/camera network API """

import json
import logging
import traceback
import base64
import hashlib
import uuid
import ssl

from datetime           import datetime, timedelta
from typing             import Optional, Any
from .                  import typings
from .software_version  import SoftwareVersion
from .exceptions        import CredentialsInvalidError, SnapshotIsNotValidFileTypeError, InvalidContentTypeError, ApiError
from xml.etree          import ElementTree as XML

import asyncio
import aiohttp
import urllib.parse as parse

from . import templates

MANUFACTURER                    = "Reolink"
DEFAULT_USE_SSL                 = False
DEFAULT_STREAM                  = "sub"
DEFAULT_PROTOCOL                = "rtmp"
DEFAULT_TIMEOUT                 = 60
DEFAULT_STREAM_FORMAT           = "h264"
DEFAULT_RTMP_AUTH_METHOD        = 'PASSWORD'
SUBSCRIPTION_TERMINATION_TIME   = 15

MOTION_DETECTION_TYPE   = "motion"
FACE_DETECTION_TYPE     = "face"
PERSON_DETECTION_TYPE   = "person"
VEHICLE_DETECTION_TYPE  = "vehicle"
PET_DETECTION_TYPE      = "pet"
VISITOR_DETECTION_TYPE  = "visitor"

_LOGGER         = logging.getLogger(__name__)
_LOGGER_DATA    = logging.getLogger(__name__+".data")

#ref_sw_version_3_0_0_0_0 = SoftwareVersion("v3.0.0.0_0")
#ref_sw_version_3_1_0_0_0 = SoftwareVersion("v3.1.0.0_0")

SSL_CONTEXT=ssl.create_default_context()
SSL_CONTEXT.set_ciphers("DEFAULT")
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE


##########################################################################################################################################################
# API class
##########################################################################################################################################################
class Host:
    """Reolink network API class."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        use_https: bool                 = DEFAULT_USE_SSL,
        protocol: str                   = DEFAULT_PROTOCOL,
        stream: str                     = DEFAULT_STREAM,
        timeout: int                    = DEFAULT_TIMEOUT,
        stream_format: str              = DEFAULT_STREAM_FORMAT,
        rtmp_auth_method: str           = DEFAULT_RTMP_AUTH_METHOD,
        aiohttp_get_session_callback    = None):

        self._send_mutex  = asyncio.Lock()
        self._login_mutex = asyncio.Lock()

        ##############################################################################
        # Host
        self._url: str                      = ""
        self._use_https: bool               = use_https
        self._host: str                     = host
        self._external_host: Optional[str]  = None
        self._external_port: Optional[str]  = None
        self._port: int                     = port
        self._rtsp_port: Optional[int]      = None
        self._rtmp_port: Optional[int]      = None
        self._onvif_port: Optional[int]     = None
        self._rtsp_enabled: Optional[bool]  = None
        self._rtmp_enabled: Optional[bool]  = None
        self._onvif_enabled: Optional[bool] = None
        self._mac_address: Optional[str]    = None

        ##############################################################################
        # Login session
        self._username: str                     = username
        self._password: str                     = password[:31]
        self._token: Optional[str]              = None
        self._lease_time: Optional[datetime]    = None
        # Connection session
        self._timeout: Optional[aiohttp.ClientTimeout] = aiohttp.ClientTimeout(total = timeout)
        if aiohttp_get_session_callback is not None:
            self._aiohttp_get_session_callback = aiohttp_get_session_callback
            self._aiohttp_session: Optional[aiohttp.ClientSession] = None
        else:
            self._aiohttp_session: Optional[aiohttp.ClientSession] = aiohttp.ClientSession(timeout = self._timeout, connector = aiohttp.TCPConnector(ssl = SSL_CONTEXT))
            self._aiohttp_get_session_callback = None

        ##############################################################################
        # NVR (host-level) attributes
        self._is_nvr: bool                                      = False
        self._nvr_name: Optional[str]                           = None
        self._nvr_serial: Optional[str]                         = None
        self._nvr_model: Optional[str]                          = None
        self._nvr_num_channels: int                             = 0
        self._nvr_sw_version: Optional[str]                     = None
        self._nvr_sw_version_object: Optional[SoftwareVersion]  = None

        ##############################################################################
        # Channels of cameras, used in this NVR ([0] for a directly connected camera)
        self._GetChannelStatus_present: bool    = False
        self._GetChannelStatus_has_name: bool   = False
        self._channels: list[int]               = list()
        self._channel_names: dict[int, str]     = dict()
        self._channel_models: dict[int, str]    = dict()

        ##############################################################################
        # Video-stream formats
        self._stream: str           = stream
        self._protocol: str         = protocol
        self._stream_format: str    = stream_format
        self._rtmp_auth_method: str = rtmp_auth_method

        ##############################################################################
        # Presets
        self._ptz_support: dict[int, bool]          = dict()
        self._ptz_presets: dict[int, dict]          = dict()
        self._sensitivity_presets: dict[int, dict]  = dict()

        ##############################################################################
        # Saved info response-blocks
        self._hdd_info: Optional[dict]      = None
        self._local_link: Optional[dict]    = None
        self._users: Optional[dict]         = None

        ##############################################################################
        # Saved settings response-blocks
        # Host-level
        self._time_settings: Optional[dict]     = None
        self._ntp_settings: Optional[dict]      = None
        self._netport_settings: Optional[dict]  = None
        # Camera-level
        self._zoom_focus_settings: dict[int, dict]  = dict()
        self._auto_focus_settings: dict[int, dict]  = dict()
        self._isp_settings: dict[int, dict]         = dict()
        self._ftp_settings: dict[int, dict]         = dict()
        self._osd_settings: dict[int, dict]         = dict()
        self._push_settings: dict[int, dict]        = dict()
        self._enc_settings: dict[int, dict]         = dict()
        self._ptz_presets_settings: dict[int, dict] = dict()
        self._email_settings: dict[int, dict]       = dict()
        self._ir_settings: dict[int, dict]          = dict()
        self._whiteled_settings: dict[int, dict]    = dict()
        self._recording_settings: dict[int, dict]   = dict()
        self._alarm_settings: dict[int, dict]       = dict()
        self._audio_alarm_settings: dict[int, dict] = dict()

        ##############################################################################
        # States
        self._motion_detection_states: dict[int, bool]  = dict()
        self._is_ia_enabled: dict[int, bool]            = dict()
        self._is_doorbell_enabled: dict[int, bool]      = dict()

        ##############################################################################
        # Camera-level states
        # Actually these must be divided into host-level and camera-level parts.
        # BUT there is a data-normalization bug in Reolink network API: in commands/responses Host-level attributes are mixed with
        # Channel-level attributes. So if you want to obtain some host-level switch (like e.g. "ftp enabled") - you still need to supply
        # a channel-number, and will get a not-requested bulky schedule-block response for that camera among requested host-level attributes.
        self._email_enabled: dict[int, bool]                  = dict()
        self._recording_enabled: dict[int, bool]              = dict()
        self._audio_alarm_enabled: dict[int, bool]            = dict()
        self._ftp_enabled: dict[int, bool]                    = dict()
        self._push_enabled: dict[int, bool]                   = dict()
        self._audio_enabled: dict[int, bool]                  = dict()
        self._ir_enabled: dict[int, bool]                     = dict()
        self._whiteled_enabled: dict[int, bool]               = dict()
        self._whiteled_modes: dict[int, int]                  = dict()
        self._daynight_state: dict[int, str]                  = dict()
        self._backlight_state: dict[int, str]                 = dict()
        self._ai_detection_states: dict[int, dict[str, bool]] = dict()

        ##############################################################################
        # API-versions of commands
        self._api_version_getemail: Optional[int]   = None
        self._api_version_getrec: Optional[int]     = None
        self._api_version_getftp: Optional[int]     = None
        self._api_version_getpush: Optional[int]    = None
        self._api_version_getalarm: Optional[int]   = None

        self.refresh_base_url()

        ##############################################################################
        # SUBSCRIPTION managing
        self._subscribe_url: Optional[str] = None

        self._subscription_manager_url: Optional[str]           = None
        self._subscription_termination_time: Optional[datetime] = None
        self._subscription_time_difference: Optional[float]     = None
    #endof __init__()


    ##############################################################################
    # Properties
    @property
    def host(self) -> str:
        return self._host

    @property
    def external_host(self) -> Optional[str]:
        return self._external_host

    @external_host.setter
    def external_host(self, value: Optional[str]):
        self._external_host = value

    @property
    def external_port(self) -> Optional[str]:
        return self._external_port

    @external_port.setter
    def external_port(self, value: Optional[str]):
        self._external_port = value

    @property
    def port(self) -> int:
        return self._port

    @property
    def onvif_port(self) -> Optional[int]:
        return self._onvif_port

    @property
    def rtmp_port(self) -> Optional[int]:
        return self._rtmp_port

    @property
    def rtsp_port(self) -> Optional[int]:
        return self._rtsp_port

    @property
    def onvif_enabled(self) -> Optional[bool]:
        return self._onvif_enabled

    @property
    def rtmp_enabled(self) -> Optional[bool]:
        return self._rtmp_enabled

    @property
    def rtsp_enabled(self) -> Optional[bool]:
        return self._rtsp_enabled

    @property
    def mac_address(self) -> Optional[str]:
        return self._mac_address

    @property
    def serial(self) -> Optional[str]:
        return self._nvr_serial

    @property
    def is_nvr(self) -> bool:
        return self._is_nvr

    @property
    def nvr_name(self) -> Optional[str]:
        if not self._is_nvr and (self._nvr_name is None or self._nvr_name == ""):
            if len(self._channels) > 0 and self._channel_names is not None and self._channels[0] in self._channel_names:
                return self._channel_names[self._channels[0]]
            else:
                return "Unknown"
        return self._nvr_name
    #endof nvr_name()

    @property
    def sw_version(self) -> Optional[str]:
        return self._nvr_sw_version

    @property
    def model(self) -> Optional[str]:
        return self._nvr_model

    @property
    def manufacturer(self) -> str:
        return MANUFACTURER

    @property
    def num_channels(self) -> int:
        """Return the total number of channels in the NVR (should be 1 for a standalone camera, maybe 2 for DUO cameras)."""
        """ RLN8-410 has bug: reports as having 12 channels, despite it's an 8-channels NVR """
        return self._nvr_num_channels

    @property
    def num_cameras(self) -> int:
        """Return the number of channels IN USE in that NVR (should be 1 for a standalone camera, maybe 2 for DUO cameras)."""
        return len(self._channels)

    @property
    def channels(self) -> list[int]:
        """Return the list of indices of channels' in use."""
        return self._channels

    @property
    def hdd_info(self) -> Optional[dict]:
        return self._hdd_info

    @property
    def stream(self) -> str:
        return self._stream

    @stream.setter
    def stream(self, value: str):
        self._stream = value

    @property
    def stream_format(self) -> str:
        return self._stream_format

    @stream_format.setter
    def stream_format(self, value: str):
        self._stream_format = value

    @property
    def protocol(self) -> str:
        return self._protocol

    @protocol.setter
    def protocol(self, value: str):
        self._protocol = value

    @property
    def session_active(self) -> bool:
        if self._token is not None and self._lease_time > (datetime.now() + timedelta(seconds = 5)):
            return True
        return False
    #endof session_active()

    @property
    def timeout(self) -> float:
        return self._timeout.total

    @timeout.setter
    def timeout(self, value: float):
        self._timeout = aiohttp.ClientTimeout(total = value)

    @property
    def is_admin(self) -> bool:
        """Check if the user has admin authorisation."""
        if self._users is None or len(self._users) < 1:
            return False

        for user in self._users:
            if user["userName"] == self._username:
                if user["level"] == "admin":
                    _LOGGER.debug("User %s has authorisation level \"admin\".", self._username)
                    return True
                else:
                    _LOGGER.warning("User %s has authorisation level \"%s\". Only admin users can change camera settings! Switches will not work.", self._username, user["level"])
                break
        return False
    #endof is_admin()


    ##############################################################################
    # Channel-level getters/setters

    def camera_name(self, channel: int) -> Optional[str]:
        if self._channel_names is None or channel not in self._channel_names:
            return "Unknown"
        return self._channel_names[channel]
    #endof camera_name()

    def camera_model(self, channel: int) -> Optional[str]:
        if self._channel_models is None or channel not in self._channel_models:
            return "Unknown"
        return self._channel_models[channel]
    #endof camera_model()

    def motion_detected(self, channel: int) -> bool:
        """Return the motion detection state (polled)."""
        return self._motion_detection_states is not None and channel in self._motion_detection_states and self._motion_detection_states[channel]
    #endof motion_detected()


    def ai_detected(self, channel: int, object_type: Optional[str] = None):
        """Return the AI object detection state (polled)."""
        if object_type is not None:
            if self._ai_detection_states is not None and channel in self._ai_detection_states and self._ai_detection_states[channel] is not None:
                for key, value in self._ai_detection_states[channel].items():
                    if key == object_type or (object_type == PERSON_DETECTION_TYPE and key == "people") or (object_type == PET_DETECTION_TYPE and key == "dog_cat"):
                        return value
            return False
        else:
            if self._ai_detection_states is not None and channel in self._ai_detection_states and self._ai_detection_states[channel] is not None:
                return self._ai_detection_states[channel]
            return {}
    #endof ai_detected()


    def audio_alarm_enabled(self, channel: int) -> bool:
        return self._audio_alarm_enabled is not None and channel in self._audio_alarm_enabled and self._audio_alarm_enabled[channel]

    def ir_enabled(self, channel: int) -> bool:
        return self._ir_enabled is not None and channel in self._ir_enabled and self._ir_enabled[channel]

    def whiteled_enabled(self, channel: int) -> bool:
        return self._whiteled_enabled is not None and channel in self._whiteled_enabled and self._whiteled_enabled[channel]


    #FIXME: Weird Reolink's lack of data normalization, some "enabled" can be a global level, some a camera level,
    #       others - ONLY global, others ONLY camera... Lets have an option where these ambiguous are always GLOBAL...
    def ftp_enabled(self, channel: Optional[int]) -> bool:
        if channel is None:
            return self._ftp_enabled is not None and 0 in self._ftp_enabled and self._ftp_enabled[0]
        else:
            return self._ftp_enabled is not None and channel in self._ftp_enabled and self._ftp_enabled[channel]

    #FIXME: Weird Reolink's lack of data normalization, some "enabled" can be a global level, some a camera level,
    #       others - ONLY global, others ONLY camera... Lets have an option where these ambiguous are always GLOBAL...
    def email_enabled(self, channel: Optional[int]) -> bool:
        if channel is None:
            return self._email_enabled is not None and 0 in self._email_enabled and self._email_enabled[0]
        else:
            return self._email_enabled is not None and channel in self._email_enabled and self._email_enabled[channel]

    #FIXME: Weird Reolink's lack of data normalization, some "enabled" can be a global level, some a camera level,
    #       others - ONLY global, others ONLY camera... Lets have an option where these ambiguous are always GLOBAL...
    def push_enabled(self, channel: Optional[int]) -> bool:
        if channel is None:
            return self._push_enabled is not None and 0 in self._push_enabled and self._push_enabled[0]
        else:
            return self._push_enabled is not None and channel in self._push_enabled and self._push_enabled[channel]

    #FIXME: Weird Reolink's lack of data normalization, some "enabled" can be a global level, some a camera level,
    #       others - ONLY global, others ONLY camera... Lets have an option where these ambiguous are always GLOBAL...
    def recording_enabled(self, channel: Optional[int]) -> bool:
        if channel is None:
            return self._recording_enabled is not None and 0 in self._recording_enabled and self._recording_enabled[0]
        else:
            return self._recording_enabled is not None and channel in self._recording_enabled and self._recording_enabled[channel]


    def whiteled_mode(self, channel: int) -> Optional[int]:
        if self._whiteled_modes is not None and channel in self._whiteled_modes:
            return self._whiteled_modes[channel]
        else:
            return None
    #endof whiteled_mode()


    def whiteled_schedule(self, channel: int) -> Optional[dict]:
        """Return the spotlight state."""
        if self._whiteled_settings is not None and channel in self._whiteled_settings:
            return self._whiteled_settings[channel]["WhiteLed"]["LightingSchedule"]
        else:
            return None
    #endof whiteled_schedule()


    def whiteled_settings(self, channel: int) -> Optional[dict]:
        """Return the spotlight state."""
        if self._whiteled_settings is not None and channel in self._whiteled_settings:
            return self._whiteled_settings[channel]
        else:
            return None
    #endof whiteled_settings()


    def daynight_state(self, channel: int) -> Optional[str]:
        if self._daynight_state is not None and channel in self._daynight_state:
            return self._daynight_state[channel]
        else:
            return None
    #endof daynight_state()


    def backlight_state(self, channel: int) -> Optional[str]:
        if self._backlight_state is not None and channel in self._backlight_state:
            return self._backlight_state[channel]
        else:
            return None
    #endof backlight_state()


    def audio_state(self, channel: int) -> bool:
        return self._audio_enabled is not None and channel in self._audio_enabled and self._audio_enabled[channel]
    #endof audio_state()


    def audio_alarm_settings(self, channel: int) -> Optional[dict]:
        if self._audio_alarm_settings is not None and channel in self._audio_alarm_settings:
            return self._audio_alarm_settings[channel]
        else:
            return None
    #endof audio_alarm_settings()


    def ptz_presets(self, channel: int) -> dict:
        if self._ptz_presets is not None and channel in self._ptz_presets:
            return self._ptz_presets[channel]
        else:
            return {}
    #endof ptz_presets()


    def sensitivity_presets(self, channel: int) -> dict:
        if self._sensitivity_presets is not None and channel in self._sensitivity_presets:
            return self._sensitivity_presets[channel] if self._sensitivity_presets[channel] is not None else {}
        else:
            return {}
    #endof sensitivity_presets()


    def ptz_supported(self, channel: int) -> bool:
        return self._ptz_support is not None and channel in self._ptz_support and self._ptz_support[channel]
    #endof ptz_supported()


    def motion_detection_state(self, channel: int) -> bool:
        return self._motion_detection_states is not None and channel in self._motion_detection_states and self._motion_detection_states[channel]
    #endof motion_detection_state()


    def is_ia_enabled(self, channel: int) -> bool:
        """Wether or not the camera supports AI objects detection"""
        return self._is_ia_enabled is not None and channel in self._is_ia_enabled and self._is_ia_enabled[channel]
    #endof is_ia_enabled()


    def is_doorbell_enabled(self, channel: int) -> bool:
        """Wether or not the camera supports doorbell"""
        return self._is_doorbell_enabled is not None and channel in self._is_doorbell_enabled and self._is_doorbell_enabled[channel]
    #endof is_doorbell_enabled()


    def enable_https(self, enable: bool):
        self._use_https = enable
        self.refresh_base_url()
    #endof enable_https()


    def refresh_base_url(self):
        if self._use_https:
            self._url = f"https://{self._host}:{self._port}/cgi-bin/api.cgi"
        else:
            self._url = f"http://{self._host}:{self._port}/cgi-bin/api.cgi"
    #endof refresh_base_url()


    ##############################################################################
    # Methods

    async def login(self) -> bool:
        await self._login_mutex.acquire()

        try:
            if self._token is not None and self._lease_time > (datetime.now() + timedelta(seconds = 300)):
                return True

            await self.logout(True) # Ensure there would be no "max session" error

            _LOGGER.debug("Host %s:%s, trying to login with user %s...", self._host, self._port, self._username)

            body = [
                {
                    "cmd": "Login",
                    "action": 0,
                    "param": {
                        "User": {
                            "userName": self._username,
                            "password": self._password,
                        }
                    },
                }
            ]
            param = {"cmd": "Login", "token": "null"}

            response = await self.send(body, param)
            if response is None:
                _LOGGER.error("Host: %s:%s: error receiving Reolink login response.", self._host, self._port)
                return False

            try:
                json_data = json.loads(response)
                _LOGGER.debug("Got login response from %s:%s: %s", self._host, self._port, json_data)
            except (TypeError, json.JSONDecodeError) as e:
                _LOGGER.error("Host %s:%s: error translating login response: %s", self._host, self._port, e)
                return False

            if json_data is not None:
                try:
                    if json_data[0]["code"] == 0:
                        self._lease_time = datetime.now() + timedelta(seconds = float(json_data[0]["value"]["Token"]["leaseTime"]))
                        self._token = str(json_data[0]["value"]["Token"]["name"])

                        _LOGGER.debug("Logged in at host %s:%s. Leasetime %s, token %s", self._host, self._port, self._lease_time.strftime("%d-%m-%Y %H:%M"), self._token)
                        # Looks like some devices fail with not-logged-in if subsequent command sent with no delay, not sure 100% though...
                        # I've seen RLC-520A failed with 0.5s, but did not try to set more. Need to gather some more logging data from users...
                        # asyncio.sleep(0.5)
                        return True
                except Exception:
                    _LOGGER.error("Host %s:%s: login error, unknown response format.", self._host, self._port)
                    self.clear_token()
                    return False

            _LOGGER.error("Failed to login at host %s:%s.", self._host, self._port)
            return False
        finally:
            self._login_mutex.release()
    #endof login()


    async def logout(self, mutex_owned = False):
        body  = [{"cmd": "Logout", "action": 0, "param": {}}]

        if not mutex_owned:
            await self._login_mutex.acquire()

        try:
            if self._token:
                param = {"cmd": "Logout", "token": self._token}
                await self.send(body, param)
            # Reolink has a bug in some cameras' firmware: the Logout command issued without a token breaks the subsequent commands:
            # even if Login command issued AFTER that successfully returns a token, any command with that token would return "Please login first" error.
            # Thus it is not available for now to exit the previous "stuck" sessions after sudden crash or power failure:
            # Reolink has restricted amount of sessions on a device, so in such case the component would not be able to login
            # into a device before some previos session expires an hour later...
            # If Reolink fixes this and makes Logout work with login/pass pair instead of a token - this can be uncommented... 
            # else:
            #     body  = [{"cmd": "Logout", "action": 0, "param": {"User": {"userName": self._username, "password": self._password}}}]
            #     param = {"cmd": "Logout"}
            #     await self.send(body, param)

            self.clear_token()
            if self._aiohttp_session is not None:
                await self._aiohttp_session.close()
        finally:
            if not mutex_owned:
                self._login_mutex.release()
    #endof logout()


    def expire_session(self):
        if self._lease_time is not None:
            self._lease_time = datetime.now() - timedelta(seconds = 5)


    def clear_token(self):
        self._token         = None
        self._lease_time    = None
    #endof clear_token()


    async def get_switchable_capabilities(self, channel: int) -> list[str]:
        """Return the capabilities of the NVR/camera that could be switched on/off."""
        capabilities: list[str] = []

        if self._ftp_enabled is not None and channel in self._ftp_enabled and self._ftp_enabled[channel] is not None:
            capabilities.append("ftp")

        if self._push_enabled is not None and channel in self._push_enabled and self._push_enabled[channel] is not None:
            capabilities.append("push")

        if self._ir_enabled is not None and channel in self._ir_enabled and self._ir_enabled[channel] is not None:
            capabilities.append("irLights")

        if self._whiteled_enabled is not None and channel in self._whiteled_enabled and self._whiteled_enabled[channel] is not None:
            capabilities.append("spotlight")

        if self._audio_alarm_enabled is not None and channel in self._audio_alarm_enabled and self._audio_alarm_enabled[channel] is not None:
            capabilities.append("siren")

        if self._recording_enabled is not None and channel in self._recording_enabled and self._recording_enabled[channel] is not None:
            capabilities.append("recording")

        if self._email_enabled is not None and channel in self._email_enabled and self._email_enabled[channel] is not None:
            capabilities.append("email")

        if self._audio_enabled is not None and channel in self._audio_enabled and self._audio_enabled[channel] is not None:
            capabilities.append("audio")

        if self._ptz_support is not None and channel in self._ptz_support and self._ptz_support[channel]:
            capabilities.append("ptzControl")
            if self._ptz_presets is not None and channel in self._ptz_presets and len(self._ptz_presets[channel]) != 0:
                capabilities.append("ptzPresets")

        if self._sensitivity_presets is not None and channel in self._sensitivity_presets and len(self._sensitivity_presets[channel]) != 0:
            capabilities.append("sensitivityPresets")

        if self._motion_detection_states is not None and channel in self._motion_detection_states and self._motion_detection_states[channel] is not None:
            capabilities.append("motionDetection")

        if self._daynight_state is not None and channel in self._daynight_state and self._daynight_state[channel] is not None:
            capabilities.append("dayNight")

        if self._backlight_state is not None and channel in self._backlight_state and self._backlight_state[channel] is not None:
            capabilities.append("backLight")

        return capabilities
    #endof get_switchable_capabilities()


    async def get_state(self, cmd: str) -> bool:
        alarm_param     = {"channel": 0, "type": "md"}
        channels_param  = {"channel": 0}
        body            = None

        if cmd == "GetEnc":
            body = [{"cmd": "GetEnc", "action": 0, "param": channels_param}]
        elif cmd == "GetIsp":
            body = [{"cmd": "GetIsp", "action": 0, "param": channels_param}]
        elif cmd == "GetIrLights":
            body = [{"cmd": "GetIrLights", "action": 0, "param": channels_param}]
        elif cmd == "GetWhiteLed":
            body = [{"cmd": "GetWhiteLed", "action": 0, "param": channels_param}]
        elif cmd == "GetPtzPreset":
            body = [{"cmd": "GetPtzPreset", "action": 0, "param": channels_param}]
        elif cmd == "GetAutoFocus":
            body = [{"cmd": "GetAutoFocus", "action": 0, "param": channels_param}]
        elif cmd == "GetZoomFocus":
            body = [{"cmd": "GetZoomFocus", "action": 0, "param": channels_param}]
        elif cmd == "GetOsd":
            body = [{"cmd": "GetOsd", "action": 0, "param": channels_param}]
        elif cmd == "GetAlarm":
            body = [
                {
                    "cmd": "GetAlarm",
                    "action": 0,
                    "param": {"Alarm": alarm_param},
                }
            ]
        elif cmd == "GetEmail" or cmd == "GetEmailV20":
            if self._api_version_getemail == 0:
                body = [{"cmd": "GetEmail", "action": 0, "param": channels_param}]
            else:
                body = [{"cmd": "GetEmailV20", "action": 0, "param": channels_param}]
        elif cmd == "GetPush" or cmd == "GetPushV20":
            if self._api_version_getpush == 0:
                body = [{"cmd": "GetPush", "action": 0, "param": channels_param}]
            else:
                body = [{"cmd": "GetPushV20", "action": 0, "param": channels_param}]
        elif cmd == "GetFtp" or cmd == "GetFtpV20":
            if self._api_version_getftp == 0:
                body = [{"cmd": "GetFtp", "action": 0, "param": channels_param}]
            else:
                body = [{"cmd": "GetFtpV20", "action": 0, "param": channels_param}]
        elif cmd == "GetRec" or cmd == "GetRecV20":
            if self._api_version_getrec == 0:
                body = [{"cmd": "GetRec", "action": 0, "param": channels_param}]
            else:
                body = [{"cmd": "GetRecV20", "action": 0, "param": channels_param}]
        elif cmd == "GetAudioAlarm" or cmd == "GetAudioAlarmV20":
            if self._api_version_getalarm == 0:
                body = [{"cmd": "GetAudioAlarm", "action": 0, "param": channels_param}]
            else:
                body = [{"cmd": "GetAudioAlarmV20", "action": 0, "param": channels_param}]

        if body is not None:
            for c in self._channels:
                alarm_param["channel"]      = c
                channels_param["channel"]   = c

                response = await self.send(body)
                if response is None:
                    _LOGGER.error("Host: %s:%s: error obtaining channel-state response for channel %s.", self._host, self._port, c)
                    self.expire_session()
                    return False

                try:
                    json_data = json.loads(response)
                    self.map_channel_json_response(json_data, c)
                except (TypeError, json.JSONDecodeError) as e:
                    _LOGGER.error("Host: %s:%s: error translating channel-state response for channel %s: %s", self._host, self._port, c, e)
                    return False
        return True
    #endof get_state()


    async def get_states(self) -> bool:
        alarm_param     = {"channel": 0, "type": "md"}
        channels_param  = {"channel": 0}

        body = [
            {"cmd": "GetEnc", "action": 0, "param": channels_param},
            {"cmd": "GetIsp", "action": 0, "param": channels_param},
            {"cmd": "GetIrLights", "action": 0, "param": channels_param},
            {"cmd": "GetWhiteLed", "action": 0, "param": channels_param},
            {"cmd": "GetPtzPreset", "action": 0, "param": channels_param},
            {"cmd": "GetAutoFocus", "action": 0, "param": channels_param},
            {"cmd": "GetZoomFocus", "action": 0, "param": channels_param},
            {"cmd": "GetOsd", "action": 0, "param": channels_param},
            {
                "cmd": "GetAlarm",
                "action": 0,
                "param": {"Alarm": alarm_param},
            }
        ]

        if self._api_version_getemail >= 1:
            body.append({"cmd": "GetEmailV20", "action": 0, "param": channels_param})
        else:
            body.append({"cmd": "GetEmail", "action": 0, "param": channels_param})

        if self._api_version_getpush >= 1:
            body.append({"cmd": "GetPushV20", "action": 0, "param": channels_param})
        else:
            body.append({"cmd": "GetPush", "action": 0, "param": channels_param})

        if self._api_version_getftp >= 1:
            body.append({"cmd": "GetFtpV20", "action": 0, "param": channels_param})
        else:
            body.append({"cmd": "GetFtp", "action": 0, "param": channels_param})

        if self._api_version_getrec >= 1:
            body.append({"cmd": "GetRecV20", "action": 0, "param": channels_param})
        else:
            body.append({"cmd": "GetRec", "action": 0, "param": channels_param})

        if self._api_version_getalarm >= 1:
            body.append({"cmd": "GetAudioAlarmV20", "action": 0, "param": channels_param})
        else:
            body.append({"cmd": "GetAudioAlarm", "action": 0, "param": channels_param})

        for c in self._channels:
            alarm_param["channel"]      = c
            channels_param["channel"]   = c

            response = await self.send(body)
            if response is None:
                _LOGGER.error("Host: %s:%s: error obtaining channel-state response for channel %s.", self._host, self._port, c)
                self.expire_session()
                return False

            try:
                json_data = json.loads(response)
                self.map_channel_json_response(json_data, c)
            except (TypeError, json.JSONDecodeError) as e:
                _LOGGER.error("Host: %s:%s: error translating channel-state response for channel %s: %s", self._host, self._port, c, e)
                return False
        return True
    #endof get_states()


    async def get_host_data(self) -> bool:
        """Fetch the host settings/capabilities."""
        body = [
            {"cmd": "Getchannelstatus"},
            {"cmd": "GetDevInfo", "action": 0, "param": {}},
            {"cmd": "GetLocalLink", "action": 0, "param": {}},
            {"cmd": "GetNetPort", "action": 0, "param": {}},
            {"cmd": "GetHddInfo", "action": 0, "param": {}},
            {"cmd": "GetUser", "action": 0, "param": {}},
            {"cmd": "GetNtp", "action": 0, "param": {}},
            {"cmd": "GetTime", "action": 0, "param": {}},
            {
                "cmd": "GetAbility",
                "action": 0,
                "param": {"User": {"userName": self._username}},
            }
        ]

        response = await self.send(body)
        if response is None:
            _LOGGER.error("Host: %s:%s: error obtaining host-settings response.", self._host, self._port)
            self.expire_session()
            return False

        try:
            json_data = json.loads(response)
            self.map_host_json_response(json_data)
        except (TypeError, json.JSONDecodeError) as e:
            _LOGGER.error("Host %s:%s: error translating host-settings response: %s", self._host, self._port, e)
            return False

        channels_param = {"channel": 0}
        channel_level_body = [
            {"cmd": "GetAiState", "action": 0, "param": channels_param}  # to capture AI capabilities
        ]
        # checking API versions (because Reolink dev quality sucks big time we cannot fully trust GetAbility)
        if self._api_version_getemail >= 1:
            channel_level_body.append({"cmd": "GetEmailV20", "action": 0, "param": channels_param})
        if self._api_version_getpush >= 1:
            channel_level_body.append({"cmd": "GetPushV20", "action": 0, "param": channels_param})
        if self._api_version_getftp >= 1:
            channel_level_body.append({"cmd": "GetFtpV20", "action": 0, "param": channels_param})
        if self._api_version_getrec >= 1:
            channel_level_body.append({"cmd": "GetRecV20", "action": 0, "param": channels_param})
        if self._api_version_getalarm >= 1:
            channel_level_body.append({"cmd": "GetAudioAlarmV20", "action": 0, "param": channels_param})

        versions_check = True
        for c in self._channels:
            channels_param["channel"] = c

            response = await self.send(channel_level_body)
            if response is None:
                _LOGGER.error("Host: %s:%s: error obtaining API response for channel %s.", self._host, self._port, c)
                self.expire_session()
                return False

            try:
                json_data = json.loads(response)
                self.map_channel_json_response(json_data, c)
            except (TypeError, json.JSONDecodeError) as e:
                _LOGGER.error("Host %s:%s: error translating API response for channel %s: %s", self._host, self._port, c, e)
                return False

            # Let's assume all channels of an NVR or multichannel-camera always have the same versions of commands... Not sure though...
            if versions_check:
                def check_command_exists(cmd: str) -> bool:
                    for x in json_data:
                        if x["cmd"] == cmd:
                            return True
                    return False

                if self._api_version_getemail >= 1:
                    if not check_command_exists("GetEmailV20"):
                        self._api_version_getemail = 0

                if self._api_version_getpush >= 1:
                    if not check_command_exists("GetPushV20"):
                        self._api_version_getpush = 0

                if self._api_version_getftp >= 1:
                    if not check_command_exists("GetFtpV20"):
                        self._api_version_getftp = 0

                if self._api_version_getrec >= 1:
                    if not check_command_exists("GetRecV20"):
                        self._api_version_getrec = 0

                if self._api_version_getalarm >= 1:
                    if not check_command_exists("GetAudioAlarmV20"):
                        self._api_version_getalarm = 0

                versions_check = False

        return True
    #endof get_host_data()


    async def get_motion_state(self, channel: int) -> Optional[bool]:
        if channel not in self._channels:
            return None

        body = [{"cmd": "GetMdState", "action": 0, "param": {"channel": channel}}]

        response = await self.send(body)
        if response is None:
            _LOGGER.error("Host %s:%s: error obtaining motion state response for channel %s.", self._host, self._port, channel)
            self.expire_session()
            self._motion_detection_states[channel] = False
            return self._motion_detection_states[channel]

        try:
            json_data = json.loads(response)
            if json_data is None:
                _LOGGER.error("Host %s:%s: unable to get motion detection state for channel %s.", self._host, self._port, channel)
                self._motion_detection_states[channel] = False
                return self._motion_detection_states[channel]

            self.map_channel_json_response(json_data, channel)
        except (TypeError, json.JSONDecodeError) as e:
            _LOGGER.error("Host %s:%s: error translating motion detection state response for channel %s: %s", self._host, self._port, channel, e)
            self._motion_detection_states[channel] = False

        return None if self._motion_detection_states is None or channel not in self._motion_detection_states or self._motion_detection_states[channel] is None else self._motion_detection_states[channel]
    #endof get_motion_state()


    async def get_ai_state(self, channel: int) -> Optional[dict[str, bool]]:
        if channel not in self._channels:
            return None

        body = [{"cmd": "GetAiState", "action": 0, "param": {"channel": channel}}]

        response = await self.send(body)
        if response is None:
            _LOGGER.error("Host %s:%s: error obtaining AI detection state response for channel %s.", self._host, self._port, channel)
            self.expire_session()
            self._ai_detection_states[channel] = None
            return self._ai_detection_states[channel]

        try:
            json_data = json.loads(response)

            if json_data is None:
                _LOGGER.error("Host %s:%s: unable to get AI detection state for channel %s.", self._host, self._port, channel)
                self._ai_detection_states[channel] = None
                return self._ai_detection_states[channel]

            self.map_channel_json_response(json_data, channel)
        except (TypeError, json.JSONDecodeError) as e:
            _LOGGER.error("Host %s:%s: error translating AI detection state response for channel %s: %s", self._host, self._port, channel, e)
            self._ai_detection_states[channel] = None

        return None if self._ai_detection_states is None or channel not in self._ai_detection_states or self._ai_detection_states[channel] is None else self._ai_detection_states[channel]
    #endof get_ai_state()


    async def get_all_motion_states(self, channel: int) -> Optional[bool]:
        """Fetch All motions states at once (regular + AI)."""
        if channel not in self._channels:
            return None

        body = [{"cmd": "GetMdState", "action": 0, "param": {"channel": channel}},
                {"cmd": "GetAiState", "action": 0, "param": {"channel": channel}}]

        response = await self.send(body)
        if response is None:
            _LOGGER.error("Host %s:%s: error obtaining All Motion States response for channel %s.", self._host, self._port, channel)
            self.expire_session()
            self._motion_detection_states[channel] = False
            self._ai_detection_states[channel] = None
            return False

        try:
            json_data = json.loads(response)
            if json_data is None:
                _LOGGER.error("Host %s:%s: unable to get All Motion States for channel %s.", self._host, self._port, channel)
                self._motion_detection_states[channel] = False
                self._ai_detection_states[channel] = None
                return False

            self.map_channel_json_response(json_data, channel)
        except (TypeError, json.JSONDecodeError) as e:
            _LOGGER.error("Host %s:%s: error translating All Motion States response for channel %s: %s", self._host, self._port, channel, e)
            self._motion_detection_states[channel] = False
            self._ai_detection_states[channel] = None

        return None if self._motion_detection_states is None or channel not in self._motion_detection_states or self._motion_detection_states[channel] is None else self._motion_detection_states[channel]
    #endof get_all_motion_states()


    async def get_snapshot(self, channel: int) -> Optional[list]:
        """Get the still image."""
        if channel not in self._channels:
            return None

        param = {"cmd": "Snap", "channel": channel}

        response = await self.send(None, param, expected_content_type = 'image/jpeg')
        if response is None or response == b"":
            _LOGGER.error("Host: %s:%s: error obtaining still image response for channel %s.", self._host, self._port, channel)
            self.expire_session()
            return None

        return response
    #endof get_snapshot()


    def get_rtmp_stream_source(self, channel: int) -> Optional[str]:
        if channel not in self._channels:
            return None

        stream_type = None
        if self._stream == "sub":
            stream_type = 1
        else:
            stream_type = 0
        if self._rtmp_auth_method == DEFAULT_RTMP_AUTH_METHOD:
            password = parse.quote(self._password)
            return f"rtmp://{self._host}:{self._rtmp_port}/bcs/channel{channel}_{self._stream}.bcs?channel={channel}&stream={stream_type}&user={self._username}&password={password}"

        return f"rtmp://{self._host}:{self._rtmp_port}/bcs/channel{channel}_{self._stream}.bcs?channel={channel}&stream={stream_type}&token={self._token}"
    #endof get_rtmp_stream_source()


    def get_rtsp_stream_source(self, channel: int) -> Optional[str]:
        if channel not in self._channels:
            return None

        password = parse.quote(self._password)
        channel = "{:02d}".format(channel + 1)
        return f"rtsp://{self._username}:{password}@{self._host}:{self._rtsp_port}/{self._stream_format}Preview_{channel}_{self._stream}"
    #endof get_rtsp_stream_source()


    async def get_stream_source(self, channel: int) -> Optional[str]:
        """Return the stream source url."""
        if not await self.login():
            return None

        if self._protocol == "rtmp":
            return self.get_rtmp_stream_source(channel)
        elif self._protocol == "rtsp":
            return self.get_rtsp_stream_source(channel)
        else:
            return None
    #endof get_stream_source()


    async def get_vod_source(self, channel: int, filename: str, external_url: bool = False) -> tuple[Optional[str], Optional[str]]:
        """Return the VOD source url."""
        if channel not in self._channels:
            return None, None
        if not await self.login():
            return None, None

        host_url: str = None
        if external_url and self._external_host:
            host_url = self._external_host
        else:
            host_url = self._host

        host_port: str = None
        if external_url and self._external_port:
            host_port = self._external_port
        else:
            host_port = self._port

        if self._is_nvr:
            # NVR VoDs "type=0": Adobe flv
            #return "video/x-flv", f"http://{host_url}:{host_port}/flv?port=1935&app=bcs&stream=playback.bcs&channel={channel}&type=0&start={filename}&seek=0&user={self._username}&password={self._password}"
            # NVR VoDs "type=1": mp4
            # return "video/mp4", f"http://{host_url}:{host_port}/flv?port=1935&app=bcs&stream=playback.bcs&channel={channel}&type=1&start={filename}&seek=0&user={self._username}&password={self._password}"
            if self._use_https:
                return "application/x-mpegURL", f"https://{host_url}:{host_port}/flv?port=1935&app=bcs&stream=playback.bcs&channel={channel}&type=1&start={filename}&seek=0&user={self._username}&password={self._password}"
            else:
                return "application/x-mpegURL", f"http://{host_url}:{host_port}/flv?port=1935&app=bcs&stream=playback.bcs&channel={channel}&type=1&start={filename}&seek=0&user={self._username}&password={self._password}"
        else:
            if external_url:
                if self._use_https:
                    return "application/x-mpegURL", f"https://{host_url}:{host_port}/cgi-bin/api.cgi?&cmd=Playback&channel={channel}&source={filename}&user={self._username}&password={self._password}"
                else:
                    return "application/x-mpegURL", f"http://{host_url}:{host_port}/cgi-bin/api.cgi?&cmd=Playback&channel={channel}&source={filename}&user={self._username}&password={self._password}"
            else:
                stream_type = None
                if self._stream == "sub":
                    stream_type = 1
                else:
                    stream_type = 0
                # Reolink uses an odd encoding, if the camera provides a / in the filename it needs to be encoded with %20
                # Camera VoDs are only available over rtmp, rtsp is not an option
                file = filename.replace('/', '%20')
                # Looks like it only works with login/password method
                # return f"rtmp://{self._host}:{self._rtmp_port}/vod/{file}?channel={channel}&stream={stream_type}&token={self._token}"
                return "application/x-mpegURL", f"rtmp://{self._host}:{self._rtmp_port}/vod/{file}?channel={channel}&stream={stream_type}&user={self._username}&password={self._password}"
    #endof get_vod_source()


    def map_host_json_response(self, json_data):
        """Map the JSON objects to internal cache-objects."""
        for data in json_data:
            try:
                if data["code"] == 1:  # Error, like "ability error"
                    continue

                if data["cmd"] == "GetChannelstatus":
                    # Maybe later add a support of dynamic cameras' connect/disconnect, without API consumer re-init.
                    # A callback from here to the API consumer would be needed I think if changes are seen.
                    if not self._GetChannelStatus_present and (self._nvr_num_channels == 0 or len(self._channels) == 0):
                        self._channels.clear()
                        self._channel_models.clear()
                        self._is_doorbell_enabled.clear()

                        cur_value = data["value"]
                        self._nvr_num_channels = cur_value["count"]

                        if self._nvr_num_channels > 0:
                            cur_status = cur_value["status"]

                            # Not all Reolink devices respond with "name" attribute.
                            if "name" in cur_status[0]:
                                self._GetChannelStatus_has_name = True
                                self._channel_names.clear()
                            else:
                                self._GetChannelStatus_has_name = False
                        
                            for ch_info in cur_status:
                                if ch_info["online"] == 1:
                                    cur_channel = ch_info["channel"]
                                    
                                    if self._GetChannelStatus_has_name:
                                        self._channel_names[cur_channel] = ch_info["name"]

                                    self._channel_models[cur_channel]       = ch_info.get("typeInfo", "Unknown") # Not all Reolink devices respond with "typeInfo" attribute.
                                    self._is_doorbell_enabled[cur_channel]  = "Doorbell" in self._channel_models[cur_channel]
                                    self._channels.append(cur_channel)
                        else:
                            self._channel_names.clear()
                    elif self._GetChannelStatus_has_name:
                        cur_status = data["value"]["status"]
                        for ch_info in cur_status:
                            if ch_info["online"] == 1:
                                # Just a dynamic name change is OK for the current "non dynamic" behavior.
                                self._channel_names[ch_info["channel"]] = ch_info["name"]

                    if not self._GetChannelStatus_present:
                        self._GetChannelStatus_present = True

                    break

            except Exception as e:  # pylint: disable=bare-except
                _LOGGER.error("Host %s:%s failed mapping JSON data: %s, traceback:\n%s\n", self._host, self._port, e, traceback.format_exc())
                continue

        for data in json_data:
            try:
                if data["code"] == 1:  # Error, like "ability error"
                    continue

                if data["cmd"] == "GetDevInfo":
                    dev_info = data["value"]["DevInfo"]
                    self._is_nvr                = dev_info.get("exactType", "CAM") == "NVR"
                    self._nvr_serial            = dev_info["serial"]
                    self._nvr_name              = dev_info["name"]
                    self._nvr_sw_version        = dev_info["firmVer"]
                    self._nvr_model: str        = dev_info["model"]
                    self._nvr_sw_version_object = SoftwareVersion(self._nvr_sw_version)

                    # In case the "GetChannelStatus" command not supported by the device.
                    if not self._GetChannelStatus_present and self._nvr_num_channels == 0:
                        self._channels.clear()
                        self._channel_models.clear()
                        self._is_doorbell_enabled.clear()

                        self._nvr_num_channels = dev_info["channelNum"]

                        if self._is_nvr:
                            _LOGGER.warning("Your %s NVR doesn't support the \"Getchannelstatus\" command. Probably you need to update your firmware.\nNo way to recognize active channels, all %s channels will be considered \"active\" as a result.", self._nvr_name, self._nvr_num_channels)

                        if self._nvr_num_channels > 0:
                            is_doorbell = "Doorbell" in self._nvr_model
                            for i in range(self._nvr_num_channels):
                                self._channel_models[i]         = self._nvr_model
                                self._is_doorbell_enabled[i]    = is_doorbell
                                self._channels.append(i)
                        else:
                            self._channel_names.clear()

                elif data["cmd"] == "GetHddInfo":
                    self._hdd_info = data["value"]["HddInfo"]

                elif data["cmd"] == "GetLocalLink":
                    self._local_link = data["value"]
                    self._mac_address = data["value"]["LocalLink"]["mac"]

                elif data["cmd"] == "GetNetPort":
                    self._netport_settings = data["value"]
                    net_port = self._netport_settings["NetPort"]
                    self._rtsp_port     = net_port["rtspPort"]
                    self._rtmp_port     = net_port["rtmpPort"]
                    self._onvif_port    = net_port["onvifPort"]
                    self._rtsp_enabled  = net_port.get("rtspEnable", 1) == 1
                    self._rtmp_enabled  = net_port.get("rtmpEnable", 1) == 1
                    self._onvif_enabled = net_port.get("onvifEnable", 1) == 1
                    self._subscribe_url = f"http://{self._host}:{self._onvif_port}/onvif/event_service"

                elif data["cmd"] == "GetUser":
                    self._users = data["value"]["User"]

                elif data["cmd"] == "GetNtp":
                    self._ntp_settings = data["value"]

                elif data["cmd"] == "GetTime":
                    self._time_settings = data["value"]

                elif data["cmd"] == "GetAbility":
                    host_abilities: dict[str, Any] = data["value"]["Ability"]
                    for ability, details in host_abilities.items():
                        if ability == 'email':
                            self._api_version_getemail = details['ver']
                        elif ability == 'push':
                            self._api_version_getpush = details['ver']
                        elif ability == 'supportFtpEnable':
                            self._api_version_getftp = details['ver']
                        elif ability == 'supportRecordEnable':
                            self._api_version_getrec = details['ver']
                        elif ability == 'supportAudioAlarm':
                            self._api_version_getalarm = details['ver']

                    if self._api_version_getemail is None:
                        self._api_version_getemail = 1

                    if self._api_version_getpush is None:
                        self._api_version_getpush = 1

                    channel_abilities: list = host_abilities["abilityChn"]
                    for channel in self._channels:
                        self._ptz_support[channel] = channel_abilities[channel]["ptzCtrl"]["permit"] != 0
                        if self._api_version_getftp is None:
                            self._api_version_getftp = channel_abilities[channel].get("ftp", {"ver": None})["ver"]
                        if self._api_version_getrec is None:
                            self._api_version_getrec = channel_abilities[channel].get("recCfg", {"ver": None})["ver"]
                        if self._api_version_getalarm is None:
                            self._api_version_getalarm = channel_abilities[channel].get("supportAudioAlarm", {"ver": None})["ver"]

                    # Channel-level in older firmwares?..
                    if self._api_version_getftp is None:
                        self._api_version_getftp = 1

                    if self._api_version_getrec is None:
                        self._api_version_getrec = 1

                    if self._api_version_getalarm is None:
                        self._api_version_getalarm = 1

            except Exception as e:  # pylint: disable=bare-except
                _LOGGER.error("Host %s:%s failed mapping JSON data: %s, traceback:\n%s\n", self._host, self._port, e, traceback.format_exc())
                continue
    #enfof map_host_json_response()


    def map_channel_json_response(self, json_data, channel: int):
        """Map the JSON objects to internal cache-objects."""
        for data in json_data:
            try:
                if data["code"] == 1:  # -->Error, like "ability error"
                    continue

                if data["cmd"] == "GetMdState":
                    self._motion_detection_states[channel] = data["value"]["state"] == 1

                elif data["cmd"] == "GetAiState":
                    self._is_ia_enabled[channel] = True
                    self._ai_detection_states[channel] = {}
                    found_channel = False
                    for key, value in data["value"].items():
                        if not found_channel and key == "channel" and value == channel:
                            found_channel = True
                            continue
                        if key == "channel" and value > channel:
                            break

                        if isinstance(value, int):  # compatibility with firmware < 3.0.0-494
                            self._ai_detection_states[channel][key] = value == 1
                        else:
                            # from firmware 3.0.0.0-494 there is a new json structure:
                            # [
                            #     {
                            #         "cmd" : "GetAiState",
                            #         "code" : 0,
                            #         "value" : {
                            #             "channel" : 0,
                            #             "face" : {
                            #                 "alarm_state" : 0,
                            #                 "support" : 0
                            #             },
                            #             "people" : {
                            #                 "alarm_state" : 0,
                            #                 "support" : 1
                            #             },
                            #             "vehicle" : {
                            #                 "alarm_state" : 0,
                            #                 "support" : 1
                            #             }
                            #         }
                            #     }
                            # ]
                            self._ai_detection_states[channel][key] = value.get('support', 0) == 1 and value.get('alarm_state', 0) == 1

                elif data["cmd"] == "GetOsd":
                    self._osd_settings[channel] = data["value"]
                    if not self._GetChannelStatus_present or not self._GetChannelStatus_has_name:
                        self._channel_names[channel] = data["value"]["Osd"]["osdChannel"]["name"]

                elif data["cmd"] == "GetFtp":
                    self._ftp_settings[channel] = data["value"]
                    self._ftp_enabled[channel] = data["value"]["Ftp"]["schedule"]["enable"] == 1

                elif data["cmd"] == "GetFtpV20":
                    self._ftp_settings[channel] = data["value"]
                    self._ftp_enabled[channel] = data["value"]["Ftp"]["enable"] == 1

                elif data["cmd"] == "GetPush":
                    self._push_settings[channel] = data["value"]
                    self._push_enabled[channel] = data["value"]["Push"]["schedule"]["enable"] == 1

                elif data["cmd"] == "GetPushV20":
                    self._push_settings[channel] = data["value"]
                    self._push_enabled[channel] = data["value"]["Push"]["enable"] == 1

                elif data["cmd"] == "GetEnc":
                    self._enc_settings[channel] = data["value"]
                    self._audio_enabled[channel] = data["value"]["Enc"]["audio"] == 1

                elif data["cmd"] == "GetEmail":
                    self._email_settings[channel] = data["value"]
                    self._email_enabled[channel] = data["value"]["Email"]["schedule"]["enable"] == 1

                elif data["cmd"] == "GetEmailV20":
                    self._email_settings[channel] = data["value"]
                    self._email_enabled[channel] = data["value"]["Email"]["enable"] == 1

                elif data["cmd"] == "GetIsp":
                    self._isp_settings[channel] = data["value"]
                    self._daynight_state[channel] = data["value"]["Isp"]["dayNight"]
                    self._backlight_state[channel] = data["value"]["Isp"]["backLight"]

                elif data["cmd"] == "GetIrLights":
                    self._ir_settings[channel] = data["value"]
                    self._ir_enabled[channel] = data["value"]["IrLights"]["state"] == "Auto"

                elif data["cmd"] == "GetWhiteLed":
                    self._whiteled_settings[channel] = data["value"]
                    self._whiteled_enabled[channel] = data["value"]["WhiteLed"]["state"] == 1
                    self._whiteled_modes[channel] = data["value"]["WhiteLed"]["mode"]

                elif data["cmd"] == "GetRec":
                    self._recording_settings[channel] = data["value"]
                    self._recording_enabled[channel] = data["value"]["Rec"]["schedule"]["enable"] == 1

                elif data["cmd"] == "GetRecV20":
                    self._recording_settings[channel] = data["value"]
                    self._recording_enabled[channel] = data["value"]["Rec"]["enable"] == 1

                elif data["cmd"] == "GetPtzPreset":
                    self._ptz_presets_settings[channel] = data["value"]
                    self._ptz_presets[channel] = {}
                    for preset in data["value"]["PtzPreset"]:
                        if int(preset["enable"]) == 1:
                            preset_name = preset["name"]
                            preset_id = int(preset["id"])
                            self._ptz_presets[channel][preset_name] = preset_id

                elif data["cmd"] == "GetAlarm":
                    self._alarm_settings[channel] = data["value"]
                    self._motion_detection_states[channel] = data["value"]["Alarm"]["enable"] == 1
                    self._sensitivity_presets[channel] = data["value"]["Alarm"]["sens"]

                elif data["cmd"] == "GetAudioAlarm":
                    self._audio_alarm_settings[channel] = data["value"]
                    self._audio_alarm_enabled[channel] = data["value"]["Audio"]["schedule"]["enable"] == 1

                elif data["cmd"] == "GetAudioAlarmV20":
                    self._audio_alarm_settings[channel] = data["value"]
                    self._audio_alarm_enabled[channel] = data["value"]["Audio"]["enable"] == 1

                elif data["cmd"] == "GetAutoFocus":
                    self._auto_focus_settings[channel] = data["value"]

                elif data["cmd"] == "GetZoomFocus":
                    self._zoom_focus_settings[channel] = data["value"]

            except Exception as e:
                _LOGGER.error("Host %s:%s (channel %s) failed mapping JSON data: %s, traceback:\n%s\n", self._host, self._port, channel, e, traceback.format_exc())
                continue
    #endof map_channel_json_response()

    async def set_net_port(self, enable_onvif: bool = None, enable_rtmp: bool = None, enable_rtsp: bool = None) -> bool:
        """Set Network Port parameters on the host (NVR or camera)."""
        if self._netport_settings is None:
            _LOGGER.error("Host %s:%s: NetPort settings are not yet available, run get_host_data first.", self._host, self._port)
            return False

        body = [{"cmd": "SetNetPort", "param": self._netport_settings}]

        if enable_onvif is not None:
            body[0]["param"]["NetPort"]["onvifEnable"] = 1 if enable_onvif else 0
        if enable_rtmp is not None:
            body[0]["param"]["NetPort"]["rtmpEnable"] = 1 if enable_rtmp else 0
        if enable_rtsp is not None:
            body[0]["param"]["NetPort"]["rtspEnable"] = 1 if enable_rtsp else 0

        response = await self.send_setting(body)
        self.expire_session() # When changing network port settings, tokens are invalidated.
        
        return response
    #endof set_net_port()

    async def set_time(self, dateFmt = None, hours24 = None, tzOffset = None) -> bool:
        """Set time on the host (NVR or camera)."""
        """Arguments:"""
        """dateFmt (string) Format of the date in the OSD timestamp"""
        """hours24 (boolean) True selects 24h format, False selects 12h format"""
        """tzoffset (int) Timezone offset versus UTC in seconds"""

        """ Always get current time first """
        ret = await self.get_host_data()
        if not ret or self._time_settings is None:
            _LOGGER.error("Host %s:%s: time settings are not available.", self._host, self._port)
            return False

        body = [{"cmd": "SetTime", "action": 0, "param": self._time_settings}]

        if dateFmt is not None:
            if dateFmt == 'DD/MM/YYYY' or dateFmt == 'MM/DD/YYYY' or dateFmt == 'YYYY/MM/DD':
                body[0]["param"]["Time"]["timeFmt"] = dateFmt
            else:
                _LOGGER.error("Invalid date format specified.")
                return False

        if hours24 is not None:
            if hours24:
                body[0]["param"]["Time"]["hourFmt"] = 0
            else:
                body[0]["param"]["Time"]["hourFmt"] = 1

        if tzOffset is not None:
            if type(tzOffset) is not int:
                _LOGGER.error("Invalid time zone offset specified, type is not integer.")
                return False
            if tzOffset < -43200 or tzOffset > 50400:
                _LOGGER.error("Invalid time zone offset specified.")
                return False
            body[0]["param"]["Time"]["timeZone"] = tzOffset

        return await self.send_setting(body)
    #endof set_time()


    async def set_ntp(self, enable = None, server = None, port = None, interval = None) -> bool:
        """Set NTP parameters on the host (NVR or camera)."""
        """Arguments:"""
        """enable (boolean) Enable synchronization"""
        """server (string) Name or IP-Address of time server (or pool)"""
        """port (int) Port number in range of (1..65535)"""
        """interval (int) Interval of synchronization in minutes in range of (60-65535)"""
        if self._ntp_settings is None:
            _LOGGER.error("Host %s:%s: NTP settings are not available.", self._host, self._port)
            return False

        body = [{"cmd": "SetNtp", "action": 0, "param": self._ntp_settings}]

        if enable is not None:
            if enable:
                body[0]["param"]["Ntp"]["enable"] = 1
            else:
                body[0]["param"]["Ntp"]["enable"] = 0

        if server is not None:
            body[0]["param"]["Ntp"]["server"] = server

        if port is not None:
            if type(port) is not int:
                _LOGGER.error("Invalid NTP port specified, type is not integer.")
                return False
            if port < 1 or port > 65535:
                _LOGGER.error("Invalid NTP port (within invalid range) specified.")
                return False
            body[0]["param"]["Ntp"]["port"] = port

        if interval is not None:
            if type(interval) is not int:
                _LOGGER.error("Invalid NTP interval specified, type is not integer.")
                return False
            if port < 60 or port > 65535:
                _LOGGER.error("Invalid NTP interval (within invalid range) specified.")
                return False
            body[0]["param"]["Ntp"]["interval"] = interval

        return await self.send_setting(body)
    #endof set_ntp()


    async def sync_ntp(self) -> bool:
        """Sync date and time on the host via NTP now."""
        if self._ntp_settings is None:
            _LOGGER.error("Host %s:%s: NTP settings are not available.", self._host, self._port)
            return False

        body = [{"cmd": "SetNtp", "action": 0, "param": self._ntp_settings}]
        body[0]["param"]["Ntp"]["interval"] = 0

        return await self.send_setting(body)
    #endof sync_ntp()


    async def set_autofocus(self, channel: int, enable: bool) -> bool:
        """Enable/Disable AutoFocus on a camera."""
        """Parameters:"""
        """enable (boolean) enables/disables AutoFocus if supported"""
        if channel not in self._channels:
            return False
        if self._auto_focus_settings is None or channel not in self._auto_focus_settings or not self._auto_focus_settings[channel]:
            _LOGGER.error("AutoFocus on camera %s is not available.", self.camera_name(channel))
            return False

        body = [{"cmd": "SetAutoFocus", "action": 0, "param": self._auto_focus_settings[channel]}]
        body[0]["param"]["AutoFocus"]["disable"] = 0 if enable else 1

        return await self.send_setting(body)
    #endof set_autofocus()


    def get_focus(self, channel: int):
        """Get absolute focus value."""
        if channel not in self._channels:
            return False
        if self._zoom_focus_settings is None or channel not in self._zoom_focus_settings or not self._zoom_focus_settings[channel]:
            _LOGGER.error("ZoomFocus on camera %s is not available.", self.camera_name(channel))
            return False

        return self._zoom_focus_settings[channel]["ZoomFocus"]["focus"]["pos"]
    #endof get_focus()


    async def set_focus(self, channel: int, focus) -> bool:
        """Set absolute focus value."""
        """Parameters:"""
        """focus (int) 0..223"""
        if channel not in self._channels:
            return False
        if not focus in range(0, 223):
            _LOGGER.error("Focus value not in range 0..223.")
            return False

        body = [{"cmd": "StartZoomFocus", "action": 0, "param": {"ZoomFocus": {"channel": channel, "op": "FocusPos", "pos": focus}}}]

        return await self.send_setting(body)
    #endof set_focus()


    def get_zoom(self, channel: int):
        """Get absolute zoom value."""
        if channel not in self._channels:
            return False
        if self._zoom_focus_settings is None or channel not in self._zoom_focus_settings or not self._zoom_focus_settings[channel]:
            _LOGGER.error("ZoomFocus on camera %s is not available.", self.camera_name(channel))
            return False

        return self._zoom_focus_settings[channel]["ZoomFocus"]["zoom"]["pos"]
    #enfof get_zoom()


    async def set_zoom(self, channel: int, zoom) -> bool:
        """Set absolute zoom value."""
        """Parameters:"""
        """zoom (int) 0..33"""
        if channel not in self._channels:
            return False
        if not zoom in range(0, 33):
            _LOGGER.error("Zoom value not in range 0..33.")
            return False

        body = [{"cmd": "StartZoomFocus", "action": 0, "param": {"ZoomFocus": {"channel": channel, "op": "ZoomPos", "pos": zoom}}}]

        return await self.send_setting(body)
    #endof set_zoom()


    def validate_osd_pos(self, pos) -> bool:
        """Helper function for validating an OSD position"""
        """Returns True, if a valid position is specified"""
        return (
            pos == "Upper Left" or pos == "Upper Right"
            or pos == "Top Center" or pos == "Bottom Center"
            or pos == "Lower Left" or pos == "Lower Right"
            )
    #endof validate_osd_pos()


    async def set_osd(self, channel: int, namePos = None, datePos = None, enableWaterMark = None) -> bool:
        """Set OSD parameters."""
        """Parameters:"""
        """namePos (string) specifies the position of the camera name - "Off" disables this OSD"""
        """datePos (string) specifies the position of the date - "Off" disables this OSD"""
        """enableWaterMark (boolean) enables/disables the Logo (WaterMark) if supported"""
        if channel not in self._channels:
            return False
        if self._osd_settings is None or channel not in self._osd_settings or not self._osd_settings[channel]:
            _LOGGER.error("OSD on camera %s is not available.", self.camera_name(channel))
            return False

        body = [{"cmd": "SetOsd", "action": 0, "param": self._osd_settings[channel]}]

        if namePos is not None:
            if namePos == "Off":
                body[0]["param"]["Osd"]["osdChannel"]["enable"] = 0
            else:
                if not self.validate_osd_pos(namePos):
                    _LOGGER.error("Invalid name OSD position specified: %s.", namePos)
                    return False
                body[0]["param"]["Osd"]["osdChannel"]["enable"] = 1
                body[0]["param"]["Osd"]["osdChannel"]["pos"] = namePos

        if datePos is not None:
            if datePos == "Off":
                body[0]["param"]["Osd"]["osdTime"]["enable"] = 0
            else:
                if not self.validate_osd_pos(datePos):
                    _LOGGER.error("Invalid date OSD position specified: %s", datePos)
                    return False
                body[0]["param"]["Osd"]["osdTime"]["enable"] = 1
                body[0]["param"]["Osd"]["osdTime"]["pos"] = datePos

        if enableWaterMark is not None:
            if "watermark" in  body[0]["param"]["Osd"]:
                if enableWaterMark:
                    body[0]["param"]["Osd"]["watermark"] = 1
                else:
                    body[0]["param"]["Osd"]["watermark"] = 0
            else:
                _LOGGER.debug("Ignoring \"enable watermark\" request. Not supported by camera %s.", self.camera_name(channel))

        return await self.send_setting(body)
    #endof set_osd()


    #FIXME: Weird Reolink's lack of data normalization, some "enabled" can be a global level, some a camera level,
    #       others - ONLY global, others ONLY camera... Lets have an option where these ambiguous are always GLOBAL...
    async def set_push(self, channel: Optional[int], enable: bool) -> bool:
        """Set the PUSH-notifications parameter."""

        body = None
        if channel is None:
            if self._api_version_getpush == 0:
                OK = True
                for c in self._channels:
                    if self._push_settings is not None and c in self._push_settings and self._push_settings[c] is not None:
                        body = [{"cmd": "SetPush", "action": 0, "param": self._push_settings[c]}]
                        body[0]["param"]["Push"]["schedule"]["enable"] = 1 if enable else 0
                        OK = OK and await self.send_setting(body)
                return OK
            else:
                body = [{"cmd": "SetPushV20", "action": 0, "param": self._push_settings[0]}]
                body[0]["param"]["Push"]["enable"] = 1 if enable else 0
                return await self.send_setting(body)
        else:
            if channel not in self._channels:
                return False
            if self._push_settings is None or channel not in self._push_settings or not self._push_settings[channel]:
                _LOGGER.error("Push-notifications on camera %s are not available.", self.camera_name(channel))
                return False

            if self._api_version_getpush == 0:
                body = [{"cmd": "SetPush", "action": 0, "param": self._push_settings[channel]}]
                body[0]["param"]["Push"]["schedule"]["enable"] = 1 if enable else 0
            else:
                body = [{"cmd": "SetPushV20", "action": 0, "param": self._push_settings[channel]}]
                body[0]["param"]["Push"]["enable"] = 1 if enable else 0

            return await self.send_setting(body)
    #endof set_push()


    #FIXME: Weird Reolink's lack of data normalization, some "enabled" can be a global level, some a camera level,
    #       others - ONLY global, others ONLY camera... Lets have an option where these ambiguous are always GLOBAL...
    async def set_ftp(self, channel: Optional[int], enable: bool) -> bool:
        """Set the FTP-notifications parameter."""

        body = None
        if channel is None:
            if self._api_version_getftp == 0:
                OK = True
                for c in self._channels:
                    if self._ftp_settings is not None and c in self._ftp_settings and self._ftp_settings[c] is not None:
                        body = [{"cmd": "SetFtp", "action": 0, "param": self._ftp_settings[c]}]
                        body[0]["param"]["Ftp"]["schedule"]["enable"] = 1 if enable else 0
                        OK = OK and await self.send_setting(body)
                return OK
            else:
                body = [{"cmd": "SetFtpV20", "action": 0, "param": self._ftp_settings[0]}]
                body[0]["param"]["Ftp"]["enable"] = 1 if enable else 0
                return await self.send_setting(body)
        else:
            if channel not in self._channels:
                return False
            if self._ftp_settings is None or channel not in self._ftp_settings or not self._ftp_settings[channel]:
                _LOGGER.error("FTP on camera %s is not available.", self.camera_name(channel))
                return False

            if self._api_version_getftp == 0:
                body = [{"cmd": "SetFtp", "action": 0, "param": self._ftp_settings[channel]}]
                body[0]["param"]["Ftp"]["schedule"]["enable"] = 1 if enable else 0
            else:
                body = [{"cmd": "SetFtpV20", "action": 0, "param": self._ftp_settings[channel]}]
                body[0]["param"]["Ftp"]["enable"] = 1 if enable else 0

            return await self.send_setting(body)
    #endof set_ftp()


    #FIXME: Weird Reolink's lack of data normalization, some "enabled" can be a global level, some a camera level,
    #       others - ONLY global, others ONLY camera... Lets have an option where these ambiguous are always GLOBAL...
    async def set_email(self, channel: Optional[int], enable: bool) -> bool:
        body = None
        if channel is None:
            if self._api_version_getemail == 0:
                OK = True
                for c in self._channels:
                    if self._email_settings is not None and c in self._email_settings and self._email_settings[c] is not None:
                        body = [{"cmd": "SetEmail", "action": 0, "param": self._email_settings[c]}]
                        body[0]["param"]["Email"]["schedule"]["enable"] = 1 if enable else 0
                        OK = OK and await self.send_setting(body)
                return OK
            else:
                body = [{"cmd": "SetEmailV20", "action": 0, "param": self._email_settings[0]}]
                body[0]["param"]["Email"]["enable"] = 1 if enable else 0
                return await self.send_setting(body)
        else:
            if channel not in self._channels:
                return False
            if self._email_settings is None or channel not in self._email_settings or not self._email_settings[channel]:
                _LOGGER.error("Email on camera %s is not available.", self.camera_name(channel))
                return False

            if self._api_version_getemail == 0:
                body = [{"cmd": "SetEmail", "action": 0, "param": self._email_settings[channel]}]
                body[0]["param"]["Email"]["schedule"]["enable"] = 1 if enable else 0
            else:
                body = [{"cmd": "SetEmailV20", "action": 0, "param": self._email_settings[channel]}]
                body[0]["param"]["Email"]["enable"] = 1 if enable else 0

            return await self.send_setting(body)
    #endof set_email()


    async def set_audio(self, channel: int, enable: bool) -> bool:
        if channel not in self._channels:
            return False
        if self._enc_settings is None or channel not in self._enc_settings or not self._enc_settings[channel]:
            _LOGGER.error("Audio on camera %s is not available.", self.camera_name(channel))
            return False

        body = [{"cmd": "SetEnc", "action": 0, "param": self._enc_settings[channel]}]
        body[0]["param"]["Enc"]["audio"] = 1 if enable else 0

        return await self.send_setting(body)
    #endof set_audio()


    async def set_ir_lights(self, channel: int, enable: bool) -> bool:
        if channel not in self._channels:
            return False
        if self._ir_settings is None or channel not in self._ir_settings or not self._ir_settings[channel]:
            _LOGGER.error("IR light on camera %s is not available.", self.camera_name(channel))
            return False

        body = [{"cmd": "SetIrLights", "action": 0, "param": {"IrLights": {"channel": channel, "state": "dummy"}}}]
        body[0]["param"]["IrLights"]["state"] = "Auto" if enable else "Off"

        return await self.send_setting(body)
    #endof set_ir_lights()


    async def set_whiteled(self, channel: int, enable: bool, brightness, mode = None) -> bool:
        """Set the WhiteLed parameter."""
        """ with Reolink Duo GetWhiteLed returns an error state """
        """ SetWhiteLed appears to require 4 parameters """
        """  state - two values 0/1 possibly OFF/ON """
        """  channel - appears to default to 0 """
        """  mode - three values I think """
        """    0  Night Mode Off """
        """    1  Night Mode On , AUTO on """
        """    3  Night Mode On, Set Time On """
        """  brightness - brigtness level range 0 to 100 """
        """                                              """
        """   TO BE CONFIRMED """
        """   There may be an extra set of parameters with Duo - dont know with others """
        """   LightingSchedule : { EndHour , EndMin, StartHour,StartMin  }    """
        """                                                                            """
        if channel not in self._channels:
            return False
        if self._whiteled_settings is None or channel not in self._whiteled_settings or not self._whiteled_settings[channel]:
            _LOGGER.error("White Led on camera %s is not available.", self.camera_name(channel))
            return False

        if mode is None:
            mode = 1
        if (brightness < 0 or brightness > 100 or (not (mode == 0 or mode == 1 or mode == 3))):
            _LOGGER.error("Incorrect parameters supplied to \"set whiteLed\": brightness = %s\n mode = %s", brightness, mode)
            return False

        body = [
            {"cmd": "SetWhiteLed",
                "param": {"WhiteLed": {"state": 1 if enable else 0, "channel": channel, "mode": mode, "bright": brightness}}
            }
        ]

        return await self.send_setting(body)
    #endof set_whiteled()


    async def set_spotlight_lighting_schedule(self, channel: int, endhour = 6, endmin = 0, starthour = 18, startmin = 0) -> bool:
        """Stub to handle setting the time period where spotlight (WhiteLed) will be on when NightMode set and AUTO is off."""
        """Time in 24-hours format"""
        if channel not in self._channels:
            return False
        if self._whiteled_settings is None or channel not in self._whiteled_settings or not self._whiteled_settings[channel]:
            _LOGGER.error("White Led on camera %s is not available.", self.camera_name(channel))
            return False

        if (endhour < 0 or endhour > 23
            or endmin < 0 or endmin > 59
            or starthour < 0 or starthour > 23
            or startmin < 0  or startmin > 59
            or (endhour == starthour and endmin < startmin)
            or (not (endhour < 12 and starthour > 16) and (endhour < starthour))):
            _LOGGER.error("Parameter error when setting Lighting schedule on camera %s: start time: %s:%s, end time: %s:%s.", self.camera_name(channel), starthour, startmin, endhour, endmin)
            return False

        body = [
            {"cmd": "SetWhiteLed",
             "param": {
                 "WhiteLed": {
                     "LightingSchedule": {
                         "EndHour": endhour, "EndMin": endmin, "StartHour": starthour, "StartMin": startmin},"channel": channel, "mode": 3
                 }
             }
             }
        ]

        return await self.send_setting(body)
    #endof set_spotlight_lighting_schedule()


    async def set_spotlight(self, channel: int, enable: bool) -> bool:
        """Simply calls set_whiteled with brightness 100, mode 3 after setting lightning schedule to on all the time 0000 to 2359."""
        if enable:
            if not await self.set_spotlight_lighting_schedule(channel, 23, 59, 0, 0):
                return False
            return await self.set_whiteled(channel, enable, 100, 3)
        else:
            if not await self.set_spotlight_lighting_schedule(channel, 0, 0, 0, 0):
                return False
            return await self.set_whiteled(channel, enable, 100, 1)
    #endof set_spotlight()


    async def set_audio_alarm(self, channel: int, enable: bool, *args) -> bool:
        # fairly basic only either turns it off or on
        # called in its simple form by set_siren
        # future version might have more parameters related to MD, AI etc
        # this information will be passed in *args or should it be a **kwargs??

        if channel not in self._channels:
            return False
        if self._audio_alarm_settings is None or channel not in self._audio_alarm_settings or not self._audio_alarm_settings[channel]:
            _LOGGER.error("AudioAlarm on camera %s is not available.", self.camera_name(channel))
            return False

        if self._api_version_getalarm == 0:
            body = [{"cmd": "SetAudioAlarm", "param": {"Audio": {"schedule": {"enable": 1 if enable else 0, "channel": channel}}}}]
        else:
            body = [{"cmd": "SetAudioAlarmV20", "param": {"Audio": {"enable": 1 if enable else 0, "channel": channel}}}]

        return await self.send_setting(body)
    #enfof set_audio_alarm()


    async def set_siren(self, channel: int, enable: bool) -> bool:
        # Uses API AudioAlarmPlay with manual switch
        # uncertain if there may be a glitch - dont know if there is API I have yet to find
        # which sets AudioLevel
        if channel not in self._channels:
            return False

        # This is overkill but to get state set right necessary to call set_audio_alarm.
        if not await self.set_audio_alarm(channel, enable):
            return False

        body = [
            {"cmd": "AudioAlarmPlay",
                "action": 0,
                "param": {
                    "alarm_mode": "manual",
                    "manual_switch": 1 if enable else 0,
                    "times": 2,
                    "channel": channel
                }
            }
        ]

        return await self.send_setting(body)
    #endof set_siren()


    async def set_daynight(self, channel: int, value: str) -> bool:
        if channel not in self._channels:
            return False
        if self._isp_settings is None or channel not in self._isp_settings or not self._isp_settings[channel]:
            _LOGGER.error("ISP on camera %s is not available.", self.camera_name(channel))
            return False

        if value not in ["Auto", "Color", "Black&White"]:
            _LOGGER.error("Invalid input for \"set day-night\": %s", value)
            return False

        body = [{"cmd": "SetIsp", "action": 0, "param": self._isp_settings[channel]}]
        body[0]["param"]["Isp"]["dayNight"] = value

        return await self.send_setting(body)
    #endof set_daynight()


    async def set_backlight(self, channel: int, value: str) -> bool:
        if channel not in self._channels:
            return False
        if self._isp_settings is None or channel not in self._isp_settings or not self._isp_settings[channel]:
            _LOGGER.error("ISP on camera %s is not available.", self.camera_name(channel))
            return False

        if value not in ["BackLightControl", "DynamicRangeControl", "Off"]:
            _LOGGER.error("Invalid input for \"set backlight\": %s", value)
            return False

        body = [{"cmd": "SetIsp", "action": 0, "param": self._isp_settings[channel]}]
        body[0]["param"]["Isp"]["backLight"] = value

        return await self.send_setting(body)
    #endof set_backlight()


    #FIXME: Weird Reolink's lack of data normalization, some "enabled" can be a global level, some a camera level,
    #       others - ONLY global, others ONLY camera... Lets have an option where these ambiguous are always GLOBAL...
    async def set_recording(self, channel: Optional[int], enable: bool) -> bool:
        """Set the recording parameter."""

        body = None
        if channel is None:
            if self._api_version_getrec == 0:
                OK = True
                for c in self._channels:
                    if self._recording_settings is not None and c in self._recording_settings and self._recording_settings[c] is not None:
                        body = [{"cmd": "SetRec", "action": 0, "param": self._recording_settings[c]}]
                        body[0]["param"]["Rec"]["schedule"]["enable"] = 1 if enable else 0
                        OK = OK and await self.send_setting(body)
                return OK
            else:
                body = [{"cmd": "SetRecV20", "action": 0, "param": self._recording_settings[0]}]
                body[0]["param"]["Rec"]["enable"] = 1 if enable else 0
                return await self.send_setting(body)
        else:
            if channel not in self._channels:
                return False
            if self._recording_settings is None or channel not in self._recording_settings or not self._recording_settings[channel]:
                _LOGGER.error("Recording on camera %s is not available.", self.camera_name(channel))
                return False

            if self._api_version_getrec == 0:
                body = [{"cmd": "SetRec", "action": 0, "param": self._recording_settings[channel]}]
                body[0]["param"]["Rec"]["schedule"]["enable"] = 1 if enable else 0
            else:
                body = [{"cmd": "SetRecV20", "action": 0, "param": self._recording_settings[channel]}]
                body[0]["param"]["Rec"]["enable"] = 1 if enable else 0

                return await self.send_setting(body)
    #endof set_recording()


    async def set_motion_detection(self, channel: int, enable: bool) -> bool:
        """Set the motion detection parameter."""
        if channel not in self._channels:
            return False
        if self._alarm_settings is None or channel not in self._alarm_settings or not self._alarm_settings[channel]:
            _LOGGER.error("Alarm on camera %s is not available.", self.camera_name(channel))
            return False

        body = [{"cmd": "SetAlarm", "action": 0, "param": self._alarm_settings[channel]}]
        body[0]["param"]["Alarm"]["enable"] = 1 if enable else 0

        return await self.send_setting(body)
    #endof set_motion_detection()


    async def set_sensitivity(self, channel: int, value: int, preset = None) -> bool:
        """Set motion detection sensitivity.
        Here the camera web and windows application show a completely different value than set.
        So the calculation <51 - value> makes the "real" value.
        """
        if channel not in self._channels:
            return False
        if self._alarm_settings is None or channel not in self._alarm_settings or not self._alarm_settings[channel]:
            _LOGGER.error("Alarm on camera %s is not available.", self.camera_name(channel))
            return False

        body = [
            {
                "cmd": "SetAlarm",
                "action": 0,
                "param": {
                    "Alarm": {
                        "channel": channel,
                        "type": "md",
                        "sens": self._alarm_settings[channel]["Alarm"]["sens"],
                    }
                }
            }
        ]
        for setting in body[0]["param"]["Alarm"]["sens"]:
            if preset is None or preset == setting["id"]:
                setting["sensitivity"] = int(51 - value)

        return await self.send_setting(body)
    #endof set_sensitivity()


    async def set_ptz_command(self, channel: int, command, preset = None, speed = None) -> bool:
        """Send PTZ command to the camera.

        List of possible commands
        --------------------------
        Command     Speed   Preset
        --------------------------
        Right       X
        RightUp     X
        RightDown   X
        Left        X
        LeftUp      X
        LeftDown    X
        Up          X
        Down        X
        ZoomInc     X
        ZoomDec     X
        FocusInc    X
        FocusDec    X
        ToPos       X       X
        Auto
        Stop
        """

        if channel not in self._channels:
            return False
        body = [{"cmd": "PtzCtrl", "action": 0, "param": {"channel": channel, "op": command}}]

        if speed:
            body[0]["param"]["speed"] = speed
        if preset:
            body[0]["param"]["id"] = preset

        return await self.send_setting(body)
    #endof set_ptz_command()


    async def request_vod_files(self, channel: int, start: datetime, end: datetime, status_only: bool = False) -> tuple[list[typings.SearchStatus], Optional[list[typings.SearchFile]]]:
        """Send search VOD-files command."""
        if channel not in self._channels:
            return None, None

        body = [
            {
                "cmd": "Search",
                "action": 0,
                "param": {
                    "Search": {
                        "channel": channel,
                        "onlyStatus": 1 if status_only else 0,
                        "streamType": self._stream,
                        "StartTime": {
                            "year": start.year,
                            "mon":  start.month,
                            "day":  start.day,
                            "hour": start.hour,
                            "min":  start.minute,
                            "sec":  start.second,
                        },
                        "EndTime": {
                            "year": end.year,
                            "mon":  end.month,
                            "day":  end.day,
                            "hour": end.hour,
                            "min":  end.minute,
                            "sec":  end.second,
                        }
                    }
                }
            }
        ]

        # response = await self.send(body, {"cmd": "Search", "rs": "000000", "user": self._username, "password": self._password})
        # response = await self.send(body, {"cmd": "Search", "user": self._username, "password": self._password})
        response = await self.send(body, {"cmd": "Search", "token": self._token})
        if response is None:
            _LOGGER.error("Host %s:%s: error receiving response for \"Search\" command.", self._host, self._port)
            self.expire_session()
            return None, None

        try:
            json_data = json.loads(response)
        except (TypeError, json.JSONDecodeError) as e:
            _LOGGER.error("Host %s:%s: error translating of \"Search\" command response to json: %s", self._host, self._port, e)
            return None, None
        except KeyError as e:
            _LOGGER.error("Host %s:%s: received an unexpected response while sending \"Search\" command: %s", self._host, self._port, e)
            return None, None

        if json_data is not None:
            if json_data[0]["code"] == 0:
                search_result = json_data[0]["value"]["SearchResult"]
                if status_only or "File" not in search_result:
                    if "Status" in search_result:
                        return search_result["Status"], None
                    else:
                        _LOGGER.info("Host: %s:%s: no \"Status\" in the result of \"Search\" command:\n%s\n", self._host, self._port, json_data)
                else:
                    return search_result["Status"], search_result["File"]
            else:
                _LOGGER.info("Host: %s:%s: the \"Search\" command returned error code %s:\n%s\n", self._host, self._port, json_data[0]["code"], json_data)
        else:
            _LOGGER.info("Host: %s:%s: failed to get results for \"Search\" command, JSON data was empty.", self._host, self._port)

        return None, None
    #endof request_vod_files()


    async def send_setting(self, body: dict) -> bool:
        command = body[0]["cmd"]
        _LOGGER.debug("Sending command: \"%s\" to: %s:%s with body: %s", command, self._host, self._port, body)

        response = await self.send(body, {"cmd": command})
        if response is None:
            _LOGGER.error("Host %s:%s: error receiving response for command \"%s\".", self._host, self._port, command)
            self.expire_session()
            return False

        try:
            json_data = json.loads(response)
            _LOGGER.debug("Response from %s:%s: %s", self._host, self._port, json_data)

            if json_data[0]["code"] == 0 and json_data[0]["value"]["rspCode"] == 200:
                if command[:3] == "Get":
                    getcmd = command.replace("Set", "Get")
                    await self.get_state(cmd = getcmd)
                return True
        except (TypeError, json.JSONDecodeError) as e:
            _LOGGER.error("Host %s:%s: error translating command \"%s\" response to json: %s", self._host, self._port, command, e)
            return False
        except KeyError as e:
            _LOGGER.error("Host %s:%s: received an unexpected response while sending command \"%s\": %s", self._host, self._port, command, e)
            return False

        _LOGGER.error("Host %s:%s: command \"%s\" error.", self._host, self._port, command)
        return False
    #endof send_setting()


    async def send(self, body, param = None, expected_content_type: Optional[str] = None, retry: bool = False) -> Optional[list]:
        """Generic send method."""

        if self._aiohttp_session is not None and self._aiohttp_session.closed:
            self._aiohttp_session = aiohttp.ClientSession(timeout=self._timeout, connector=aiohttp.TCPConnector(ssl=SSL_CONTEXT))

        is_login_logout = body is not None and (body[0]["cmd"] == "Login" or body[0]["cmd"] == "Logout")
        if not is_login_logout:
            if not await self.login():
                return None

        if not param:
            param = {}
        if self._token is not None:
            param["token"] = self._token

        try:
            session = self._aiohttp_session
            if session is None:
                session = self._aiohttp_get_session_callback()
            if body is None:
                async with self._send_mutex:
                    response = await session.get(url = self._url, params = param, allow_redirects = False)

                _LOGGER.debug("%s/%s:%s::send() HTTP Request params =\n%s\n", self.nvr_name, self._host, self._port, str(param).replace(self._password, "<password>"))
                _LOGGER.debug("%s/%s:%s::send() HTTP Response status = %s, content-type = (%s).", self.nvr_name, self._host, self._port, response.status, response.content_type)

                json_data = await response.read()
                if param.get("cmd") == "Snap":
                    _LOGGER_DATA.debug("%s/%s:%s::send() HTTP Response (snapshot) data scrapped because it's too large.", self.nvr_name, self._host, self._port)
                else:
                    _LOGGER_DATA.debug("%s/%s:%s::send() HTTP Response data:\n%s\n", self.nvr_name, self._host, self._port, json_data)

                if len(json_data) < 500 and response.content_type == 'text/html':
                    if b'"detail" : "invalid user"' in json_data or b'"detail" : "login failed"' in json_data or b'detail" : "please login first' in json_data:
                        if is_login_logout:
                            raise CredentialsInvalidError()
                        else:
                            if retry:
                                raise CredentialsInvalidError()
                            _LOGGER.debug("Host %s:%s: \"invalid login\" response, trying to login again and retry the command.", self._host, self._port)
                            self.expire_session()
                            return await self.send(body, param, expected_content_type, retry = True)

                if response.status >= 400:
                    raise ApiError("API returned HTTP status ERROR code {}/{}".format(response.status, response.reason))

                if expected_content_type is not None and response.content_type != expected_content_type:
                    raise InvalidContentTypeError("Expected type '{}' but received '{}'.".format(expected_content_type, response.content_type))

                return json_data
            else:
                async with self._send_mutex:
                    response = await session.post(url = self._url, json = body, params = param, allow_redirects = False)

                _LOGGER.debug("%s/%s:%s::send() HTTP Request params =\n%s\n", self.nvr_name, self._host, self._port, str(param).replace(self._password, "<password>"))
                _LOGGER.debug("%s/%s:%s::send() HTTP Request body =\n%s\n", self.nvr_name, self._host, self._port, str(body).replace(self._password, "<password>"))
                _LOGGER.debug("%s/%s:%s::send() HTTP Response status = %s, content-type = (%s).", self.nvr_name, self._host, self._port, response.status, response.content_type)

                json_data = await response.text()
                if param.get("cmd") == "Search" and len(json_data) > 500:
                    _LOGGER_DATA.debug("%s/%s:%s::send() HTTP Response (VOD search) data scrapped because it's too large.", self.nvr_name, self._host, self._port)
                else:
                    _LOGGER_DATA.debug("%s/%s:%s::send() HTTP Response data:\n%s\n", self.nvr_name, self._host, self._port, json_data)

                if len(json_data) < 500 and response.content_type == 'text/html':
                    if ('"detail" : "invalid user"' in json_data or '"detail" : "login failed"' in json_data or 'detail" : "please login first' in json_data) and body[0]["cmd"] != "Logout":
                        if is_login_logout:
                            raise CredentialsInvalidError()
                        else:
                            if retry:
                                raise CredentialsInvalidError()
                            _LOGGER.debug("Host %s:%s: \"invalid login\" response, trying to login again and retry the command.", self._host, self._port)
                            self.expire_session()
                            return await self.send(body, param, expected_content_type, retry = True)                     

                if response.status >= 400:
                    raise ApiError("API returned HTTP status ERROR code {}/{}.".format(response.status, response.reason))

                return json_data
        except aiohttp.ClientConnectorError as e:
            self.expire_session()
            _LOGGER.error("Host %s:%s: connection error: %s", self._host, self._port, str(e))
            raise e
        except asyncio.TimeoutError as e:
            self.expire_session()
            _LOGGER.error("Host %s:%s: connection timeout exception. Please check the connection to this host.", self._host, self._port)
            raise e
        except ApiError as e:
            self.expire_session()
            _LOGGER.error("Host %s:%s: API error: %s.", self._host, self._port, str(e))
            raise e
        except CredentialsInvalidError as e:
            self.expire_session()
            _LOGGER.error("Host %s:%s: login attempt failed.", self._host, self._port)
            raise e
        except InvalidContentTypeError as e:
            self.expire_session()
            _LOGGER.error("Host %s:%s: content type error: %s.", self._host, self._port, str(e))
            raise e
        except Exception as e:
            self.expire_session()
            _LOGGER.error("Host %s:%s: Unknown exception \"%s\" occurred, traceback:\n%s\n", self._host, self._port, str(e), traceback.format_exc())
            raise e
    #endof send()


    ##############################################################################
    # SUBSCRIPTION managing
    @property
    def renewtimer(self) -> int:
        """Return the renew time in seconds. Negative if expired."""
        if self._subscription_time_difference is None or self._subscription_termination_time is None:
            return -1

        diff = self._subscription_termination_time - datetime.utcnow()
        _LOGGER.debug("Host %s:%s should renew in: %i seconds...", self._host, self._port, diff)

        return diff.seconds
    #endof renewtimer()


    @property
    def subscribed(self) -> bool:
        return (self._subscription_manager_url is not None and self.renewtimer > 0)
    #endof subscribed()


    async def convert_time(self, time) -> Optional[datetime]:
        """Convert time object to printable."""
        try:
            return datetime.strptime(time, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return None
    #endof convert_time()


    async def calc_time_difference(self, local_time, remote_time):
        """Calculate the time difference between local and remote."""
        return remote_time.timestamp() - local_time.timestamp()
    #endof calc_time_difference()


    async def get_digest(self) -> dict:
        """Get the authorisation digest."""
        time_created = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")

        raw_nonce = uuid.uuid4().bytes
        nonce = base64.b64encode(raw_nonce)

        sha1 = hashlib.sha1()
        sha1.update(raw_nonce + time_created.encode("utf8") + self._password.encode("utf8"))
        raw_digest = sha1.digest()
        digest_pwd = base64.b64encode(raw_digest)

        return {
            "UsernameToken":    str(uuid.uuid4()),
            "Username":         self._username,
            "PasswordDigest":   digest_pwd.decode("utf8"),
            "Nonce":            nonce.decode("utf8"),
            "Created":          time_created,
        }
    #endof get_digest()


    async def subscription_send(self, headers, data) -> Optional[str]:
        """Send subscription data to the camera."""
        try:
            async with aiohttp.ClientSession(timeout = self._timeout, connector = aiohttp.TCPConnector(verify_ssl = False)) as session:
                _LOGGER.debug("Host %s:%s: subscription request data:\n%s\n", self._host, self._port, data)

                async with self._send_mutex:
                    response = await session.post(url = self._subscribe_url, data = data, headers = headers, allow_redirects = False)

                response_text = await response.text()
                _LOGGER.debug("Host %s:%s: subscription got response status: %s. Payload:\n%s\n", self._host, self._port, response.status, response_text)

                if response.status == 200:
                    return response_text
                else:
                    _LOGGER.warning("Host %s:%s: subscription request got a response with wrong HTTP status %s: %s", self._host, self._port, response.status, response.reason)

        except aiohttp.ClientConnectorError as e:
            _LOGGER.error('Host %s:%s: connection error: %s.', self._host, self._port, str(e))
        except asyncio.TimeoutError:
            _LOGGER.error('Host %s:%s: connection timeout exception.', self._host, self._port)
        except:
            _LOGGER.error('Host %s:%s: unknown exception occurred.', self._host, self._port)
    #endof subscription_send()


    async def subscribe(self, webhook_url: str) -> bool:
        """Subscribe to ONVIF events."""
        await self.unsubscribe_all() # Trying to free up dangling subscriptions (limited resource in case of some NVRs/cameras).

        headers = templates.HEADERS
        headers.update(templates.SUBSCRIBE_ACTION)
        template = templates.SUBSCRIBE_XML

        parameters = {
            "Address": webhook_url,
            "InitialTerminationTime": f"PT{SUBSCRIPTION_TERMINATION_TIME}M",
        }

        parameters.update(await self.get_digest())
        local_time = datetime.utcnow()

        xml = template.format(**parameters)

        response = await self.subscription_send(headers, xml)
        if response is None:
            await self.unsubscribe_all()
            return False
        root = XML.fromstring(response)

        address_element = root.find('.//{http://www.w3.org/2005/08/addressing}Address')
        if address_element is None:
            await self.unsubscribe_all()
            _LOGGER.error("Host %s:%s: failed to subscribe.", self._host, self._port)
            return False
        self._subscription_manager_url = address_element.text

        if self._subscription_manager_url is None:
            await self.unsubscribe_all()
            _LOGGER.error("Host %s:%s: failed to subscribe. Required response parameters not available.", self._host, self._port)
            return False

        current_time_element = root.find('.//{http://docs.oasis-open.org/wsn/b-2}CurrentTime')
        if current_time_element is None:
            await self.unsubscribe_all()
            _LOGGER.error("Host %s:%s: failed to subscribe.", self._host, self._port)
            return False
        remote_time = await self.convert_time(current_time_element.text)

        if remote_time is None:
            await self.unsubscribe_all()
            _LOGGER.error("Host %s:%s: failed to subscribe. Required response parameters not available.", self._host, self._port)
            return False

        self._subscription_time_difference = await self.calc_time_difference(local_time, remote_time)

        termination_time_element = root.find('.//{http://docs.oasis-open.org/wsn/b-2}TerminationTime')
        if termination_time_element is None:
            await self.unsubscribe_all()
            _LOGGER.error("Host %s:%s: failed to subscribe.", self._host, self._port)
            return False
        self._subscription_termination_time = await self.convert_time(termination_time_element.text) - timedelta(seconds = self._subscription_time_difference)

        if self._subscription_termination_time is None:
            await self.unsubscribe_all()
            _LOGGER.error("Host %s:%s: failed to subscribe. Required response parameters not available.", self._host, self._port)
            return False

        _LOGGER.debug(
            "Local time: %s, camera time: %s (difference: %s), termination time: %s",
            local_time.strftime('%Y-%m-%d %H:%M'), remote_time.strftime('%Y-%m-%d %H:%M'),
            self._subscription_time_difference, self._subscription_termination_time.strftime('%Y-%m-%d %H:%M')
        )

        return True
    #endof subscribe()


    async def renew(self) -> bool:
        """Renew the ONVIF event subscription."""

        if not self.subscribed:
            return await self.subscribe()

        headers = templates.HEADERS
        headers.update(templates.RENEW_ACTION)
        template = templates.RENEW_XML

        parameters = {
            "To": self._subscription_manager_url,
            "TerminationTime": f"PT{SUBSCRIPTION_TERMINATION_TIME}M",
        }

        parameters.update(await self.get_digest())
        local_time = datetime.utcnow()

        xml = template.format(**parameters)

        response = await self.subscription_send(headers, xml)
        if response is None:
            await self.unsubscribe_all()
            return False
        root = XML.fromstring(response)

        current_time_element = root.find('.//{http://docs.oasis-open.org/wsn/b-2}CurrentTime')
        if current_time_element is None:
            await self.unsubscribe_all()
            _LOGGER.error("Host %s:%s: failed to subscribe.", self._host, self._port)
            return False
        remote_time = await self.convert_time(current_time_element.text)

        if remote_time is None:
            await self.unsubscribe_all()
            _LOGGER.error("Host %s:%s: failed to renew subscription. Unexpected response.", self._host, self._port)
            return False

        self._subscription_time_difference = await self.calc_time_difference(local_time, remote_time)

        # The Reolink renew functionality has a bug: it always returns the INITIAL TerminationTime.
        # By adding the duration to the CurrentTime parameter, the new termination time can be calculated.
        # This will not work before the Reolink bug gets fixed on all devices
        # termination_time_element = root.find('.//{http://docs.oasis-open.org/wsn/b-2}TerminationTime')
        # if termination_time_element is None:
        #     await self.unsubscribe_all()
        #     _LOGGER.error("Host %s:%s: failed to subscribe.", self._host, self._port)
        #     return False
        # self._subscription_termination_time = await self.convert_time(termination_time_element.text) - timedelta(seconds = self._subscription_time_difference)
        self._subscription_termination_time = local_time + timedelta(minutes = SUBSCRIPTION_TERMINATION_TIME)

        if self._subscription_termination_time is None:
            await self.unsubscribe_all()
            _LOGGER.error("Host %s:%s: failed to renew subscription. Unexpected response.", self._host, self._port)
            return False

        _LOGGER.debug(
            "Local time: %s, camera time: %s (difference: %s), termination time: %s",
            local_time.strftime('%Y-%m-%d %H:%M'), remote_time.strftime('%Y-%m-%d %H:%M'),
            self._subscription_time_difference, self._subscription_termination_time.strftime('%Y-%m-%d %H:%M')
        )

        return True
    #endof renew()


    async def unsubscribe(self):
        """Unsubscribe from ONVIF events."""
        if self._subscription_manager_url is not None:
            headers = templates.HEADERS
            headers.update(templates.UNSUBSCRIBE_ACTION)
            template = templates.UNSUBSCRIBE_XML

            parameters = {"To": self._subscription_manager_url}
            parameters.update(await self.get_digest())

            xml = template.format(**parameters)

            await self.subscription_send(headers, xml)

            self._subscription_manager_url = None

        self._subscription_termination_time = None
        self._subscription_time_difference = None

        return True
    #endof unsubscribe()


    async def unsubscribe_all(self):
        """Unsubscribe from ONVIF events. Normally only needed during entry initialization/setup, to free possibly dangling subscriptions."""
        headers = templates.HEADERS
        headers.update(templates.UNSUBSCRIBE_ACTION)
        template = templates.UNSUBSCRIBE_XML

        await self.unsubscribe()

        if self._nvr_model == "RLN8-410" or self._nvr_model == "RLN16-410":
            _LOGGER.debug("Attempting to unsubscribe previous (dead) sessions notifications...")

            # These work for RLN8-410 NVR, so up to 3 maximum subscriptions on it
            parameters = {"To": f"http://{self._host}:{self._onvif_port}/onvif/Notification?Idx=00_0"}
            parameters.update(await self.get_digest())
            xml = template.format(**parameters)
            await self.subscription_send(headers, xml)

            parameters = {"To": f"http://{self._host}:{self._onvif_port}/onvif/Notification?Idx=00_1"}
            parameters.update(await self.get_digest())
            xml = template.format(**parameters)
            await self.subscription_send(headers, xml)

            parameters = {"To": f"http://{self._host}:{self._onvif_port}/onvif/Notification?Idx=00_2"}
            parameters.update(await self.get_digest())
            xml = template.format(**parameters)
            await self.subscription_send(headers, xml)

        return True
    #endof unsubscribe_all()

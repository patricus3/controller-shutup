# Controller Shutup: stop NVDA speech when a game controller button is pressed.
# Polls XInput (Xbox-style pads) and the legacy winmm joystick API (everything else)
# on a background thread and cancels speech on the rising edge of any button.

import ctypes
import threading
import time
from ctypes import wintypes

import addonHandler
import config
import globalPluginHandler
import queueHandler
import speech
import ui
from logHandler import log
from scriptHandler import script

addonHandler.initTranslation()

POLL_INTERVAL = 0.01  # seconds between polls of connected controllers
RESCAN_INTERVAL = 2.0  # seconds between checks for newly connected controllers

config.conf.spec["controllerShutup"] = {
	"enabled": "boolean(default=True)",
}


# --- XInput ---

class XINPUT_GAMEPAD(ctypes.Structure):
	_fields_ = [
		("wButtons", wintypes.WORD),
		("bLeftTrigger", ctypes.c_ubyte),
		("bRightTrigger", ctypes.c_ubyte),
		("sThumbLX", wintypes.SHORT),
		("sThumbLY", wintypes.SHORT),
		("sThumbRX", wintypes.SHORT),
		("sThumbRY", wintypes.SHORT),
	]


class XINPUT_STATE(ctypes.Structure):
	_fields_ = [
		("dwPacketNumber", wintypes.DWORD),
		("Gamepad", XINPUT_GAMEPAD),
	]


XUSER_MAX_COUNT = 4
XINPUT_GAMEPAD_TRIGGER_THRESHOLD = 30
ERROR_SUCCESS = 0
# Synthetic bits so the triggers can be treated like ordinary buttons.
LEFT_TRIGGER_BIT = 1 << 16
RIGHT_TRIGGER_BIT = 1 << 17


def _loadXInput():
	for dllName in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
		try:
			dll = ctypes.WinDLL(dllName)
		except OSError:
			continue
		func = dll.XInputGetState
		func.argtypes = (wintypes.DWORD, ctypes.POINTER(XINPUT_STATE))
		func.restype = wintypes.DWORD
		return func
	return None


# --- winmm joystick API ---

class JOYINFOEX(ctypes.Structure):
	_fields_ = [
		("dwSize", wintypes.DWORD),
		("dwFlags", wintypes.DWORD),
		("dwXpos", wintypes.DWORD),
		("dwYpos", wintypes.DWORD),
		("dwZpos", wintypes.DWORD),
		("dwRpos", wintypes.DWORD),
		("dwUpos", wintypes.DWORD),
		("dwVpos", wintypes.DWORD),
		("dwButtons", wintypes.DWORD),
		("dwButtonNumber", wintypes.DWORD),
		("dwPOV", wintypes.DWORD),
		("dwReserved1", wintypes.DWORD),
		("dwReserved2", wintypes.DWORD),
	]


JOY_RETURNPOV = 0x40
JOY_RETURNBUTTONS = 0x80
JOY_POVCENTERED = 0xFFFF
JOYERR_NOERROR = 0
# Synthetic bit for "the POV hat / d-pad is pushed in some direction".
POV_BIT = 1 << 32


def _loadWinmm():
	try:
		dll = ctypes.WinDLL("winmm")
	except OSError:
		return None, None
	getNumDevs = dll.joyGetNumDevs
	getNumDevs.argtypes = ()
	getNumDevs.restype = wintypes.UINT
	getPosEx = dll.joyGetPosEx
	getPosEx.argtypes = (wintypes.UINT, ctypes.POINTER(JOYINFOEX))
	getPosEx.restype = wintypes.UINT
	return getNumDevs, getPosEx


class ControllerPoller(threading.Thread):
	"""Watches all controllers and calls onPress whenever a new button goes down."""

	def __init__(self, onPress):
		super().__init__(name="controllerShutup.ControllerPoller", daemon=True)
		self._onPress = onPress
		self._stopEvent = threading.Event()
		self._xinputGetState = _loadXInput()
		self._joyGetNumDevs, self._joyGetPosEx = _loadWinmm()
		# Polling disconnected devices can be slow, so only connected ones are polled
		# every tick; the rest are re-probed every RESCAN_INTERVAL seconds.
		self._xinputConnected = {}  # user index -> last button mask
		self._joyConnected = {}  # joystick id -> last button mask
		self._lastRescan = 0.0

	def stop(self):
		self._stopEvent.set()

	def run(self):
		while not self._stopEvent.is_set():
			try:
				now = time.monotonic()
				if now - self._lastRescan >= RESCAN_INTERVAL:
					self._lastRescan = now
					self._rescan()
				pressed = self._pollXInput() | self._pollJoysticks()
				if pressed:
					self._onPress()
			except Exception:
				log.error("controllerShutup: error polling controllers", exc_info=True)
				self._stopEvent.wait(1.0)
			self._stopEvent.wait(POLL_INTERVAL)

	def _rescan(self):
		if self._xinputGetState:
			for index in range(XUSER_MAX_COUNT):
				if index not in self._xinputConnected:
					mask = self._readXInput(index)
					if mask is not None:
						# Seed with the current state so held buttons don't count as a press.
						self._xinputConnected[index] = mask
		if self._joyGetPosEx:
			for joyId in range(self._joyGetNumDevs()):
				if joyId not in self._joyConnected:
					mask = self._readJoystick(joyId)
					if mask is not None:
						self._joyConnected[joyId] = mask

	def _readXInput(self, index):
		state = XINPUT_STATE()
		if self._xinputGetState(index, ctypes.byref(state)) != ERROR_SUCCESS:
			return None
		pad = state.Gamepad
		mask = pad.wButtons
		if pad.bLeftTrigger > XINPUT_GAMEPAD_TRIGGER_THRESHOLD:
			mask |= LEFT_TRIGGER_BIT
		if pad.bRightTrigger > XINPUT_GAMEPAD_TRIGGER_THRESHOLD:
			mask |= RIGHT_TRIGGER_BIT
		return mask

	def _readJoystick(self, joyId):
		info = JOYINFOEX()
		info.dwSize = ctypes.sizeof(JOYINFOEX)
		info.dwFlags = JOY_RETURNBUTTONS | JOY_RETURNPOV
		if self._joyGetPosEx(joyId, ctypes.byref(info)) != JOYERR_NOERROR:
			return None
		mask = info.dwButtons
		if info.dwPOV != JOY_POVCENTERED:
			mask |= POV_BIT
		return mask

	def _pollXInput(self):
		return self._pollDevices(self._xinputConnected, self._readXInput)

	def _pollJoysticks(self):
		return self._pollDevices(self._joyConnected, self._readJoystick)

	@staticmethod
	def _pollDevices(connected, read):
		"""Returns True if any device has a button that was up last poll and is down now."""
		pressed = False
		for deviceId, oldMask in list(connected.items()):
			mask = read(deviceId)
			if mask is None:
				del connected[deviceId]
				continue
			if mask & ~oldMask:
				pressed = True
			connected[deviceId] = mask
		return pressed


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	scriptCategory = _("Controller Shutup")

	def __init__(self):
		super().__init__()
		self._poller = ControllerPoller(self._onControllerPress)
		self._poller.start()

	def terminate(self):
		self._poller.stop()
		self._poller.join(timeout=1.0)
		super().terminate()

	def _onControllerPress(self):
		if config.conf["controllerShutup"]["enabled"]:
			# Speech must be cancelled from NVDA's main thread.
			queueHandler.queueFunction(queueHandler.eventQueue, speech.cancelSpeech)

	@script(
		# Translators: Describes the toggle command in the Input Gestures dialog.
		description=_("Toggles whether pressing a game controller button stops speech"),
	)
	def script_toggleControllerShutup(self, gesture):
		section = config.conf["controllerShutup"]
		section["enabled"] = not section["enabled"]
		if section["enabled"]:
			# Translators: Announced when controller speech interruption is turned on.
			ui.message(_("Controller stops speech on"))
		else:
			# Translators: Announced when controller speech interruption is turned off.
			ui.message(_("Controller stops speech off"))

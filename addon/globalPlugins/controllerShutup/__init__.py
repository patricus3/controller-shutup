# Controller Shutup: stop NVDA speech when a game controller button is pressed.
# Polls XInput (Xbox-style pads) and the legacy winmm joystick API (everything else)
# on a background thread and cancels speech on the rising edge of any button,
# d-pad direction, trigger or analog stick direction.

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
# Presses detected this soon after one that already interrupted speech are ignored.
# One physical press is often seen twice, e.g. by both XInput and winmm a frame apart.
REPEAT_WINDOW = 0.15
# Fraction of full deflection a stick or trigger must pass to count as pressed,
# and drop back below to count as released again (hysteresis avoids jitter).
AXIS_PRESS = 0.5
AXIS_RELEASE = 0.25
# How long recent speech is remembered so it can be re-spoken after an interruption.
SPEECH_HISTORY = 2.0

config.conf.spec["controllerShutup"] = {
	"enabled": "boolean(default=True)",
}

# Bit layout of a controller's combined state mask.
POV_SHIFT = 32  # 4 bits: d-pad / POV hat up, right, down, left
AXIS_SHIFT = 40  # 2 bits per axis: positive and negative deflection


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
ERROR_SUCCESS = 0


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


class JOYCAPSW(ctypes.Structure):
	_fields_ = [
		("wMid", wintypes.WORD),
		("wPid", wintypes.WORD),
		("szPname", wintypes.WCHAR * 32),
		("wXmin", wintypes.UINT),
		("wXmax", wintypes.UINT),
		("wYmin", wintypes.UINT),
		("wYmax", wintypes.UINT),
		("wZmin", wintypes.UINT),
		("wZmax", wintypes.UINT),
		("wNumButtons", wintypes.UINT),
		("wPeriodMin", wintypes.UINT),
		("wPeriodMax", wintypes.UINT),
		("wRmin", wintypes.UINT),
		("wRmax", wintypes.UINT),
		("wUmin", wintypes.UINT),
		("wUmax", wintypes.UINT),
		("wVmin", wintypes.UINT),
		("wVmax", wintypes.UINT),
		("wCaps", wintypes.UINT),
		("wMaxAxes", wintypes.UINT),
		("wNumAxes", wintypes.UINT),
		("wMaxButtons", wintypes.UINT),
		("szRegKey", wintypes.WCHAR * 32),
		("szOEMVxD", wintypes.WCHAR * 260),
	]


JOY_RETURNALL = 0xFF  # all six axes, POV and buttons
JOY_POVCENTERED = 0xFFFF
JOYERR_NOERROR = 0


def _loadWinmm():
	try:
		dll = ctypes.WinDLL("winmm")
	except OSError:
		return None
	dll.joyGetNumDevs.argtypes = ()
	dll.joyGetNumDevs.restype = wintypes.UINT
	dll.joyGetPosEx.argtypes = (wintypes.UINT, ctypes.POINTER(JOYINFOEX))
	dll.joyGetPosEx.restype = wintypes.UINT
	dll.joyGetDevCapsW.argtypes = (ctypes.c_size_t, ctypes.POINTER(JOYCAPSW), wintypes.UINT)
	dll.joyGetDevCapsW.restype = wintypes.UINT
	return dll


# --- Controllers ---

class Controller:
	"""Tracks one controller's state and reports when something on it is newly pressed."""

	def __init__(self):
		self._mask = None

	def read(self):
		"""Returns (button mask, axis deflections from -1 to 1), or None if disconnected."""
		raise NotImplementedError

	def update(self):
		"""Returns None if disconnected, otherwise whether anything went down since the last update.
		The first update only records the current state, so held inputs don't count as a press.
		"""
		state = self.read()
		if state is None:
			return None
		buttons, axes = state
		mask = buttons | (self._axisBits(axes) << AXIS_SHIFT)
		pressed = self._mask is not None and bool(mask & ~self._mask)
		self._mask = mask
		return pressed

	def _axisBits(self, axes):
		oldBits = (self._mask or 0) >> AXIS_SHIFT
		bits = 0
		for index, value in enumerate(axes):
			for bit, deflection in ((1 << (2 * index), value), (1 << (2 * index + 1), -value)):
				threshold = AXIS_RELEASE if oldBits & bit else AXIS_PRESS
				if deflection > threshold:
					bits |= bit
		return bits


class XInputController(Controller):

	def __init__(self, getState, index):
		super().__init__()
		self._getState = getState
		self._index = index

	def read(self):
		state = XINPUT_STATE()
		if self._getState(self._index, ctypes.byref(state)) != ERROR_SUCCESS:
			return None
		pad = state.Gamepad
		axes = (
			pad.sThumbLX / 32768,
			pad.sThumbLY / 32768,
			pad.sThumbRX / 32768,
			pad.sThumbRY / 32768,
			pad.bLeftTrigger / 255,
			pad.bRightTrigger / 255,
		)
		return pad.wButtons, axes


def _povBits(pov):
	"""Maps a POV angle in hundredths of a degree to up/right/down/left bits; diagonals set two."""
	if pov == JOY_POVCENTERED:
		return 0
	bits = 0
	if pov >= 31500 or pov <= 4500:
		bits |= 1
	if 4500 <= pov <= 13500:
		bits |= 2
	if 13500 <= pov <= 22500:
		bits |= 4
	if 22500 <= pov <= 31500:
		bits |= 8
	return bits


class JoystickController(Controller):

	def __init__(self, winmm, joyId):
		super().__init__()
		self._winmm = winmm
		self._joyId = joyId
		caps = JOYCAPSW()
		if winmm.joyGetDevCapsW(joyId, ctypes.byref(caps), ctypes.sizeof(caps)) == JOYERR_NOERROR:
			ranges = (
				(caps.wXmin, caps.wXmax), (caps.wYmin, caps.wYmax), (caps.wZmin, caps.wZmax),
				(caps.wRmin, caps.wRmax), (caps.wUmin, caps.wUmax), (caps.wVmin, caps.wVmax),
			)
		else:
			ranges = ((0, 0xFFFF),) * 6
		# Half of each axis's range, for normalising deflection; unused axes report 0..0.
		self._halfRanges = [max((high - low) / 2, 1) for low, high in ranges]
		# Resting positions, taken from the first reading. Triggers mapped to axes often
		# rest at one end rather than the centre, so the centre can't be assumed.
		self._rest = None

	def read(self):
		info = JOYINFOEX()
		info.dwSize = ctypes.sizeof(JOYINFOEX)
		info.dwFlags = JOY_RETURNALL
		if self._winmm.joyGetPosEx(self._joyId, ctypes.byref(info)) != JOYERR_NOERROR:
			return None
		positions = (info.dwXpos, info.dwYpos, info.dwZpos, info.dwRpos, info.dwUpos, info.dwVpos)
		if self._rest is None:
			self._rest = positions
		axes = tuple(
			(position - rest) / halfRange
			for position, rest, halfRange in zip(positions, self._rest, self._halfRanges)
		)
		return info.dwButtons | (_povBits(info.dwPOV) << POV_SHIFT), axes


class ControllerPoller(threading.Thread):
	"""Watches all controllers and calls onPress(pressTime) whenever something new goes down.
	pressTime is the last time (time.perf_counter) all controllers were seen without that input,
	i.e. the earliest moment the press could have happened.
	"""

	def __init__(self, onPress):
		super().__init__(name="controllerShutup.ControllerPoller", daemon=True)
		self._onPress = onPress
		self._stopEvent = threading.Event()
		self._xinputGetState = _loadXInput()
		self._winmm = _loadWinmm()
		# Polling disconnected devices can be slow, so only connected ones are polled
		# every tick; the rest are re-probed every RESCAN_INTERVAL seconds.
		self._controllers = {}  # ("xinput", index) or ("joystick", id) -> Controller
		self._lastRescan = 0.0
		self._lastPoll = 0.0
		self._lastPressReported = float("-inf")

	def stop(self):
		self._stopEvent.set()

	def run(self):
		while not self._stopEvent.is_set():
			try:
				now = time.perf_counter()
				if now - self._lastRescan >= RESCAN_INTERVAL:
					self._lastRescan = now
					self._rescan()
				if self._poll() and now - self._lastPressReported >= REPEAT_WINDOW:
					self._lastPressReported = now
					self._onPress(self._lastPoll)
				self._lastPoll = now
			except Exception:
				log.error("controllerShutup: error polling controllers", exc_info=True)
				self._stopEvent.wait(1.0)
			self._stopEvent.wait(POLL_INTERVAL)

	def _rescan(self):
		candidates = []
		if self._xinputGetState:
			candidates += [
				(("xinput", index), lambda index=index: XInputController(self._xinputGetState, index))
				for index in range(XUSER_MAX_COUNT)
			]
		if self._winmm:
			candidates += [
				(("joystick", joyId), lambda joyId=joyId: JoystickController(self._winmm, joyId))
				for joyId in range(self._winmm.joyGetNumDevs())
			]
		for key, create in candidates:
			if key not in self._controllers:
				controller = create()
				if controller.update() is not None:
					self._controllers[key] = controller

	def _poll(self):
		"""Updates every connected controller; returns True if anything was newly pressed."""
		pressed = False
		for key, controller in list(self._controllers.items()):
			result = controller.update()
			if result is None:
				del self._controllers[key]
			elif result:
				pressed = True
		return pressed


def _isSayAllRunning():
	try:
		from speech.sayAll import SayAllHandler
		return SayAllHandler.isRunning()
	except (ImportError, AttributeError):
		try:
			import sayAllHandler
			return sayAllHandler.isRunning()
		except (ImportError, AttributeError):
			return False


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	scriptCategory = _("Controller Shutup")

	def __init__(self):
		super().__init__()
		# (time, sequence, symbolLevel, priority) for each recent speech.speak call.
		self._recentSpeech = []
		try:
			from speech.extensions import pre_speech
			pre_speech.register(self._onPreSpeech)
			self._preSpeech = pre_speech
		except (ImportError, AttributeError):
			# Older NVDA: no way to tell what was spoken after a press, so just cancel everything.
			self._preSpeech = None
		self._poller = ControllerPoller(self._onControllerPress)
		self._poller.start()

	def terminate(self):
		self._poller.stop()
		self._poller.join(timeout=1.0)
		if self._preSpeech:
			self._preSpeech.unregister(self._onPreSpeech)
		super().terminate()

	def _onPreSpeech(self, speechSequence=None, originalSpeechSequence=None, symbolLevel=None, priority=None, **kwargs):
		# Prefer the unfiltered sequence so speech filters aren't applied twice on re-speaking.
		sequence = originalSpeechSequence if originalSpeechSequence is not None else speechSequence
		if sequence is None:
			return
		now = time.perf_counter()
		self._recentSpeech = [entry for entry in self._recentSpeech if now - entry[0] < SPEECH_HISTORY]
		self._recentSpeech.append((now, list(sequence), symbolLevel, priority))

	def _onControllerPress(self, pressTime):
		if config.conf["controllerShutup"]["enabled"]:
			# Speech must be cancelled from NVDA's main thread.
			queueHandler.queueFunction(queueHandler.eventQueue, self._interruptSpeech, pressTime)

	def _interruptSpeech(self, pressTime):
		"""Cancels speech that was already going when the controller was pressed.
		By the time this runs, the app may already have reacted to the press and NVDA may
		have started speaking the result, so anything spoken after pressTime is spoken again.
		"""
		# Say all queues its next line by itself; re-speaking that would keep say all going.
		responses = [] if _isSayAllRunning() else [
			entry for entry in self._recentSpeech if entry[0] > pressTime
		]
		self._recentSpeech = [entry for entry in self._recentSpeech if entry[0] <= pressTime]
		speech.cancelSpeech()
		for _speechTime, sequence, symbolLevel, priority in responses:
			kwargs = {"symbolLevel": symbolLevel}
			if priority is not None:
				kwargs["priority"] = priority
			speech.speak(sequence, **kwargs)

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

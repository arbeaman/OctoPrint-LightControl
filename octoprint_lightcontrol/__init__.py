__author__ = "Alerick Beaman <35195829+arbeaman@users.noreply.github.com>"
__license__ = "GNU Affero General Public License http://www.gnu.org/licenses/agpl.html"
__copyright__ = "Copyright (C) 2026 Alerick Beaman - Released under terms of the AGPLv3 License"
# OctoPrint plugin that switches the 3D printer lights on and off. The lights can be toggled
# from the nav bar or driven automatically by G-code triggers and printer idle state, using
# G-code, a system command, a GPIO pin, or a registered sub-plugin for switching and sensing.

import glob
import platform
import subprocess
import threading
import time

import flask
import octoprint.plugin
from flask import jsonify, make_response
from flask_babel import gettext
from octoprint.access.permissions import Permissions
from octoprint.events import Events
from octoprint.settings import valid_boolean_trues
from octoprint.util import ResettableTimer, fqfn

from . import cli

try:
    import periphery
    HAS_GPIO = True
except ModuleNotFoundError:
    HAS_GPIO = False

try:
    KERNEL_VERSION = tuple([int(s) for s in platform.release().split(".")[:2]])
except ValueError:
    KERNEL_VERSION = (0, 0)

SUPPORTS_LINE_BIAS = KERNEL_VERSION >= (5, 5)


class LightControl(octoprint.plugin.StartupPlugin,
                 octoprint.plugin.TemplatePlugin,
                 octoprint.plugin.AssetPlugin,
                 octoprint.plugin.SettingsPlugin,
                 octoprint.plugin.SimpleApiPlugin,
                 octoprint.plugin.EventHandlerPlugin,
                 octoprint.plugin.WizardPlugin):
    """OctoPrint plugin providing manual and automatic control of the 3D printer lights."""

    def __init__(self):
        """Initialize plugin state and detect the available GPIO character devices."""
        self._sub_plugins = dict()
        self._availableGPIODevices = self.get_gpio_devs()

        self.config = dict()

        self._autoOnTriggerGCodeCommandsArray = []
        self._idleIgnoreCommandsArray = []
        self._check_light_state_thread = None
        self._check_light_state_event = threading.Event()
        self._idleTimer = None
        self._waitForHeaters = False
        self._skipIdleTimer = False
        self._configuredGPIOPins = {}
        self._noSensing_isLightOn = False
        self.isLightOn = False


    def get_settings_defaults(self):
        """Define the plugin's configuration keys and their default values."""
        return dict(
            GPIODevice = '',
            switchingMethod = 'GCODE',
            onoffGPIOPin = 0,
            invertonoffGPIOPin = False,
            onGCodeCommand = 'M80',
            offGCodeCommand = 'M81',
            onSysCommand = '',
            offSysCommand = '',
            switchingPlugin = '',
            enablePseudoOnOff = False,
            pseudoOnGCodeCommand = 'M80',
            pseudoOffGCodeCommand = 'M81',
            postOnDelay = 0.0,
            connectOnPowerOn = False,
            disconnectOnPowerOff = False,
            sensingMethod = 'INTERNAL',
            senseGPIOPin = 0,
            sensePollingInterval = 5,
            invertsenseGPIOPin = False,
            senseGPIOPinPUD = '',
            senseSystemCommand = '',
            sensingPlugin = '',
            autoOn = False,
            autoOnTriggerGCodeCommands = "G0,G1,G2,G3,G10,G11,G28,G29,G32,M104,M106,M109,M140,M190",
            enablePowerOffWarningDialog = False,
            powerOffWhenIdle = False,
            idleTimeout = 30,
            idleIgnoreCommands = 'M105',
            idleTimeoutWaitTemp = 50,
            turnOnWhenApiUploadPrint = False,
            turnOffWhenError = False
        )


    def on_settings_initialized(self):
        """Ensure the post-on/pre-off G-code scripts exist and load settings into memory."""
        scripts = self._settings.listScripts("gcode")

        if not "lightcontrol_post_on" in scripts:
            self._settings.saveScript("gcode", "lightcontrol_post_on", u'')

        if not "lightcontrol_pre_off" in scripts:
            self._settings.saveScript("gcode", "lightcontrol_pre_off", u'')

        self.reload_settings()


    def reload_settings(self):
        """Copy stored settings into the in-memory config, disabling options that are unavailable or incompatible."""
        for k, v in self.get_settings_defaults().items():
            if isinstance(v, str):
                v = self._settings.get([k])
            elif isinstance(v, bool):
                v = self._settings.get_boolean([k])
            elif isinstance(v, int):
                v = self._settings.get_int([k])
            elif isinstance(v, float):
                v = self._settings.get_float([k])

            self.config[k] = v
            self._logger.debug("{}: {}".format(k, v))

        if self.config['switchingMethod'] == 'GPIO' and not HAS_GPIO:
            self._logger.error("Unable to use GPIO for switchingMethod.")
            self.config['switchingMethod'] = ''

        if self.config['sensingMethod'] == 'GPIO' and not HAS_GPIO:
            self._logger.error("Unable to use GPIO for sensingMethod.")
            self.config['sensingMethod'] = ''

        if self.config['enablePseudoOnOff'] and self.config['switchingMethod'] == 'GCODE':
            self._logger.warning("Pseudo On/Off cannot be used in conjunction with GCODE switching. Disabling.")
            self.config['enablePseudoOnOff'] = False

        self._autoOnTriggerGCodeCommandsArray = self.config['autoOnTriggerGCodeCommands'].split(',')
        self._idleIgnoreCommandsArray = self.config['idleIgnoreCommands'].split(',')


    def on_after_startup(self):
        """Configure GPIO if selected, start the state-polling thread, and arm the idle timer."""
        if self.config['switchingMethod'] == 'GPIO' or self.config['sensingMethod'] == 'GPIO':
            self.configure_gpio()

        self._check_light_state_thread = threading.Thread(target=self._check_light_state)
        self._check_light_state_thread.daemon = True
        self._check_light_state_thread.start()

        self._start_idle_timer()


    def get_gpio_devs(self):
        """Return the available GPIO character devices (/dev/gpiochip*)."""
        return sorted(glob.glob('/dev/gpiochip*'))


    def cleanup_gpio(self):
        """Release any GPIO lines currently held by the plugin."""
        for k, pin in self._configuredGPIOPins.items():
            self._logger.debug("Cleaning up {} pin {}".format(k, pin.name))
            try:
                pin.close()
            except Exception:
                self._logger.exception(
                    "Exception while cleaning up {} pin {}.".format(k, pin.name)
                )
        self._configuredGPIOPins = {}


    def configure_gpio(self):
        """Open the GPIO lines used for switching and/or sensing."""
        self._logger.info("Periphery version: {}".format(periphery.version))

        if self.config['switchingMethod'] == 'GPIO':
            self._logger.info("Using GPIO for On/Off")
            self._logger.info("Configuring GPIO for pin {}".format(self.config['onoffGPIOPin']))

            if not self.config['invertonoffGPIOPin']:
                initial_output = 'low'
            else:
                initial_output = 'high'

            try:
                pin = periphery.GPIO(self.config['GPIODevice'], self.config['onoffGPIOPin'], initial_output)
                self._configuredGPIOPins['switch'] = pin
            except Exception:
                self._logger.exception(
                    "Exception while setting up GPIO pin {}".format(self.config['onoffGPIOPin'])
                )

        if self.config['sensingMethod'] == 'GPIO':
            self._logger.info("Using GPIO sensing to determine Light on/off state.")
            self._logger.info("Configuring GPIO for pin {}".format(self.config['senseGPIOPin']))


            if not SUPPORTS_LINE_BIAS:
                if self.config['senseGPIOPinPUD'] != '':
                    self._logger.warning("Kernel version 5.5 or greater required for GPIO bias. Using 'default'.")
                bias = "default"
            elif self.config['senseGPIOPinPUD'] == '':
                bias = "disable"
            elif self.config['senseGPIOPinPUD'] == 'PULL_UP':
                bias = "pull_up"
            elif self.config['senseGPIOPinPUD'] == 'PULL_DOWN':
                bias = "pull_down"
            else:
                bias = "default"

            try:
                pin = periphery.CdevGPIO(path=self.config['GPIODevice'], line=self.config['senseGPIOPin'], direction='in', bias=bias)
                self._configuredGPIOPins['sense'] = pin
            except Exception:
                self._logger.exception(
                    "Exception while setting up GPIO pin {}".format(self.config['senseGPIOPin'])
                )


    def _get_plugin_key(self, implementation):
        """Return the plugin identifier for a registered sub-plugin implementation."""
        for k, v in self._plugin_manager.plugin_implementations.items():
            if v == implementation:
                return k


    def register_plugin(self, implementation):
        """Record a sub-plugin implementation so it can be used for switching or sensing."""
        k = self._get_plugin_key(implementation)

        self._logger.debug("Registering plugin - {}".format(k))

        if k not in self._sub_plugins:
            self._logger.info("Registered plugin - {}".format(k))
            self._sub_plugins[k] = implementation


    def check_light_state(self):
        """Wake the polling thread to refresh the state immediately."""
        self._check_light_state_event.set()


    def _check_light_state(self):
        """Continuously poll the configured sensing method, publish state changes, and notify clients."""
        while True:
            old_isLightOn = self.isLightOn

            self._logger.debug("Polling Light state...")

            if self.config['sensingMethod'] == 'GPIO':
                r = 0
                try:
                    r = self._configuredGPIOPins['sense'].read()
                except Exception:
                    self._logger.exception("Exception while reading GPIO line")

                self._logger.debug("Result: {}".format(r))

                new_isLightOn = r ^ self.config['invertsenseGPIOPin']

                self.isLightOn = new_isLightOn
            elif self.config['sensingMethod'] == 'SYSTEM':
                new_isLightOn = False

                p = subprocess.Popen(self.config['senseSystemCommand'], shell=True)
                self._logger.debug("Sensing system command executed. PID={}, Command={}".format(p.pid, self.config['senseSystemCommand']))
                while p.poll() is None:
                    time.sleep(0.1)
                r = p.returncode
                self._logger.debug("Sensing system command returned: {}".format(r))

                # Sensing system command: exit code 0 means on, 1 means off.
                if r == 0:
                    new_isLightOn = True
                elif r == 1:
                    new_isLightOn = False

                self.isLightOn = new_isLightOn
            elif self.config['sensingMethod'] == 'INTERNAL':
                self.isLightOn = self._noSensing_isLightOn
            elif self.config['sensingMethod'] == 'PLUGIN':
                p = self.config['sensingPlugin']

                r = False

                if p not in self._sub_plugins:
                    self._logger.error('Plugin {} is configured for sensing but it is not registered.'.format(p))
                elif not hasattr(self._sub_plugins[p], 'get_light_state'):
                    self._logger.error('Plugin {} is configured for sensing but get_light_state is not defined.'.format(p))
                else:
                    callback = self._sub_plugins[p].get_light_state
                    try:
                        r = callback()
                    except Exception:
                        self._logger.exception(
                            "Error while executing callback {}".format(
                                callback
                            ),
                            extra={"callback": fqfn(callback)},
                        )

                self.isLightOn = r
            else:
                self.isLightOn = False

            self._logger.debug("isLightOn: {}".format(self.isLightOn))

            if (old_isLightOn != self.isLightOn):
                self._logger.debug("Light state changed, firing light_state_changed event.")

                event = Events.PLUGIN_LIGHTCONTROL_LIGHT_STATE_CHANGED
                self._event_bus.fire(event, payload=dict(isLightOn=self.isLightOn))

            if (old_isLightOn != self.isLightOn) and self.isLightOn:
                self._start_idle_timer()
            elif (old_isLightOn != self.isLightOn) and not self.isLightOn:
                self._stop_idle_timer()

            self._plugin_manager.send_plugin_message(self._identifier, dict(isLightOn=self.isLightOn))

            self._check_light_state_event.wait(self.config['sensePollingInterval'])
            self._check_light_state_event.clear()


    def _start_idle_timer(self):
        """Start the idle power-off countdown when idle power-off is enabled and the output is on."""
        self._stop_idle_timer()

        if self.config['powerOffWhenIdle'] and self.isLightOn:
            self._idleTimer = ResettableTimer(self.config['idleTimeout'] * 60, self._idle_poweroff)
            self._idleTimer.start()


    def _stop_idle_timer(self):
        """Cancel the idle power-off countdown."""
        if self._idleTimer:
            self._idleTimer.cancel()
            self._idleTimer = None


    def _reset_idle_timer(self):
        """Restart the idle power-off countdown from the beginning."""
        try:
            if self._idleTimer.is_alive():
                self._idleTimer.reset()
            else:
                raise Exception()
        except:
            self._start_idle_timer()


    def _idle_poweroff(self):
        """Switch off once the printer has been idle, unless it is printing or paused."""
        if not self.config['powerOffWhenIdle']:
            return

        if self._waitForHeaters:
            return

        if self._printer.is_printing() or self._printer.is_paused():
            return

        self._logger.info("Idle timeout reached after {} minute(s). Waiting for tool temperatures before shutting off Light.".format(self.config['idleTimeout']))
        if self._wait_for_heaters():
            self._logger.info("Heaters below temperature.")
            self.turn_light_off()
        else:
            self._logger.info("Aborted Light shut down due to activity.")


    def _wait_for_heaters(self):
        """Wait until tool (hotend) temperatures fall below the configured threshold before switching off.

        Only the light is switched off by the idle routine; the printer's own heaters are left
        untouched. The wait considers tool (hotend) temperatures only -- the bed is ignored.
        """
        self._waitForHeaters = True

        while True:
            if not self._waitForHeaters:
                return False

            heaters = self._printer.get_current_temperatures()

            highest_temp = 0
            heaters_above_waittemp = []
            for heater, entry in heaters.items():
                if not heater.startswith("tool"):
                    continue

                actual = entry.get("actual")
                if actual is None:
                    # heater doesn't exist in fw
                    continue

                try:
                    temp = float(actual)
                except ValueError:
                    # not a float for some reason, skip it
                    continue

                self._logger.debug("Heater {} = {}C".format(heater, temp))
                if temp > self.config['idleTimeoutWaitTemp']:
                    heaters_above_waittemp.append(heater)

                if temp > highest_temp:
                    highest_temp = temp

            if highest_temp <= self.config['idleTimeoutWaitTemp']:
                self._waitForHeaters = False
                return True

            self._logger.info("Waiting for heaters({}) before shutting off Light...".format(', '.join(heaters_above_waittemp)))
            time.sleep(5)


    def hook_gcode_queuing(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):
        """Handle pseudo on/off commands, auto-on triggers, and idle-timer resets for outgoing G-code."""
        skipQueuing = False

        if not gcode:
            gcode = cmd.split(' ', 1)[0]

        if self.config['enablePseudoOnOff']:
            if gcode == self.config['pseudoOnGCodeCommand']:
                self.turn_light_on()
                comm_instance._log("LightControl: ok")
                skipQueuing = True
            elif gcode == self.config['pseudoOffGCodeCommand']:
                self.turn_light_off()
                comm_instance._log("LightControl: ok")
                skipQueuing = True

        if (not self.isLightOn and self.config['autoOn'] and (gcode in self._autoOnTriggerGCodeCommandsArray)):
            self._logger.info("Auto-On - Turning Light On (Triggered by {})".format(gcode))
            self.turn_light_on()

        if self.config['powerOffWhenIdle'] and self.isLightOn and not self._skipIdleTimer:
            if not (gcode in self._idleIgnoreCommandsArray):
                self._waitForHeaters = False
                self._reset_idle_timer()

        if skipQueuing:
            return (None,)


    def turn_light_on(self):
        """Switch on using the configured method and run the post-on script."""
        if self.config['switchingMethod'] in ['GCODE', 'GPIO', 'SYSTEM', 'PLUGIN']:
            self._logger.info("Switching Light On")
            if self.config['switchingMethod'] == 'GCODE':
                self._logger.debug("Switching Light On Using GCODE: {}".format(self.config['onGCodeCommand']))
                self._printer.commands(self.config['onGCodeCommand'])
            elif self.config['switchingMethod'] == 'SYSTEM':
                self._logger.debug("Switching Light On Using SYSTEM: {}".format(self.config['onSysCommand']))

                p = subprocess.Popen(self.config['onSysCommand'], shell=True)
                self._logger.debug("On system command executed. PID={}, Command={}".format(p.pid, self.config['onSysCommand']))
                while p.poll() is None:
                    time.sleep(0.1)
                r = p.returncode

                self._logger.debug("On system command returned: {}".format(r))
            elif self.config['switchingMethod'] == 'GPIO':
                self._logger.debug("Switching Light On Using GPIO: {}".format(self.config['onoffGPIOPin']))
                pin_output = bool(1 ^ self.config['invertonoffGPIOPin'])

                try:
                    self._configuredGPIOPins['switch'].write(pin_output)
                except Exception :
                    self._logger.exception("Exception while writing GPIO line")
                    return
            elif self.config['switchingMethod'] == 'PLUGIN':
                p = self.config['switchingPlugin']
                self._logger.debug("Switching Light On Using PLUGIN: {}".format(p))

                if p not in self._sub_plugins:
                    self._logger.error('Plugin {} is configured for switching but it is not registered.'.format(p))
                    return
                elif not hasattr(self._sub_plugins[p], 'turn_light_on'):
                    self._logger.error('Plugin {} is configured for switching but turn_light_on is not defined.'.format(p))
                    return
                else:
                    callback = self._sub_plugins[p].turn_light_on
                    try:
                        r = callback()
                    except Exception:
                        self._logger.exception(
                            "Error while executing callback {}".format(
                                callback
                            ),
                            extra={"callback": fqfn(callback)},
                        )
                        return

            if self.config['sensingMethod'] not in ('GPIO', 'SYSTEM', 'PLUGIN'):
                self._noSensing_isLightOn = True

            time.sleep(0.1 + self.config['postOnDelay'])

            self.check_light_state()

            # Optionally reconnect to the printer when the lights are switched on.
            if self.config['connectOnPowerOn'] and self._printer.is_closed_or_error():
                self._printer.connect()
                time.sleep(0.1)

            if not self._printer.is_closed_or_error():
                self._printer.script("lightcontrol_post_on", must_be_set=False)


    def turn_light_off(self):
        """Run the pre-off script and switch off using the configured method."""
        if self.config['switchingMethod'] in ['GCODE', 'GPIO', 'SYSTEM', 'PLUGIN']:
            if not self._printer.is_closed_or_error():
                self._printer.script("lightcontrol_pre_off", must_be_set=False)

            self._logger.info("Switching Light Off")
            if self.config['switchingMethod'] == 'GCODE':
                self._logger.debug("Switching Light Off Using GCODE: {}".format(self.config['offGCodeCommand']))
                self._printer.commands(self.config['offGCodeCommand'])
            elif self.config['switchingMethod'] == 'SYSTEM':
                self._logger.debug("Switching Light Off Using SYSTEM: {}".format(self.config['offSysCommand']))

                p = subprocess.Popen(self.config['offSysCommand'], shell=True)
                self._logger.debug("Off system command executed. PID={}, Command={}".format(p.pid, self.config['offSysCommand']))
                while p.poll() is None:
                    time.sleep(0.1)
                r = p.returncode

                self._logger.debug("Off system command returned: {}".format(r))
            elif self.config['switchingMethod'] == 'GPIO':
                self._logger.debug("Switching Light Off Using GPIO: {}".format(self.config['onoffGPIOPin']))
                pin_output = bool(0 ^ self.config['invertonoffGPIOPin'])

                try:
                    self._configuredGPIOPins['switch'].write(pin_output)
                except Exception:
                    self._logger.exception("Exception while writing GPIO line")
                    return
            elif self.config['switchingMethod'] == 'PLUGIN':
                p = self.config['switchingPlugin']
                self._logger.debug("Switching Light Off Using PLUGIN: {}".format(p))

                if p not in self._sub_plugins:
                    self._logger.error('Plugin {} is configured for switching but it is not registered.'.format(p))
                    return
                elif not hasattr(self._sub_plugins[p], 'turn_light_off'):
                    self._logger.error('Plugin {} is configured for switching but turn_light_off is not defined.'.format(p))
                    return
                else:
                    callback = self._sub_plugins[p].turn_light_off
                    try:
                        r = callback()
                    except Exception:
                        self._logger.exception(
                            "Error while executing callback {}".format(
                                callback
                            ),
                            extra={"callback": fqfn(callback)},
                        )
                        return

            # Optionally disconnect from the printer when the lights are switched off.
            if self.config['disconnectOnPowerOff']:
                self._printer.disconnect()

            if self.config['sensingMethod'] not in ('GPIO', 'SYSTEM', 'PLUGIN'):
                self._noSensing_isLightOn = False

            time.sleep(0.1)
            self.check_light_state()


    def get_light_state(self):
        """Return the current on/off state."""
        return self.isLightOn


    def turn_on_before_printing_after_upload(self):
        """Turn the lights on when a file is uploaded and selected to print immediately via the API."""
        if ( self.config['turnOnWhenApiUploadPrint'] and
             not self.isLightOn and
             flask.request.path.startswith('/api/files/') and
             flask.request.method == 'POST' and
             flask.request.values.get('print', 'false') in valid_boolean_trues):
                self.on_api_command("turnLightOn", [])


    def on_event(self, event, payload):
        """Push state to newly connected clients and switch off on firmware/communication errors."""
        if event == Events.CLIENT_OPENED:
            self._plugin_manager.send_plugin_message(self._identifier, dict(isLightOn=self.isLightOn))
            return
        elif event == Events.ERROR and self.config['turnOffWhenError']:
            self._logger.info("Firmware or communication error detected. Turning Light Off")
            self.turn_light_off()
            return


    def is_api_protected(self):
        # Require a logged-in user for the simple API; per-command permission
        # checks are additionally enforced in on_api_command().
        return True


    def get_api_commands(self):
        """Declare the simple-API commands the plugin accepts."""
        return dict(
            turnLightOn=[],
            turnLightOff=[],
            toggleLight=[],
            getLightState=[]
        )


    def on_api_get(self, request):
        """Return the current state in response to a simple-API GET."""
        return self.on_api_command("getLightState", [])


    def on_api_command(self, command, data):
        """Enforce permissions and carry out the on/off/toggle/status API commands."""
        if command in ['turnLightOn', 'turnLightOff', 'toggleLight']:
            if not Permissions.PLUGIN_LIGHTCONTROL_CONTROL.can():
                return make_response("Insufficient rights", 403)
        elif command in ['getLightState']:
            if not Permissions.STATUS.can():
                return make_response("Insufficient rights", 403)

        if command == 'turnLightOn':
            self.turn_light_on()
        elif command == 'turnLightOff':
            self.turn_light_off()
        elif command == 'toggleLight':
            if self.isLightOn:
                self.turn_light_off()
            else:
                self.turn_light_on()
        elif command == 'getLightState':
            return jsonify(isLightOn=self.isLightOn)


    def on_settings_save(self, data):
        """Persist the G-code scripts and settings, then reconfigure GPIO and the idle timer."""
        if 'scripts_gcode_lightcontrol_post_on' in data:
            script = data["scripts_gcode_lightcontrol_post_on"]
            self._settings.saveScript("gcode", "lightcontrol_post_on", u'' + script.replace("\r\n", "\n").replace("\r", "\n"))
            data.pop('scripts_gcode_lightcontrol_post_on')

        if 'scripts_gcode_lightcontrol_pre_off' in data:
            script = data["scripts_gcode_lightcontrol_pre_off"]
            self._settings.saveScript("gcode", "lightcontrol_pre_off", u'' + script.replace("\r\n", "\n").replace("\r", "\n"))
            data.pop('scripts_gcode_lightcontrol_pre_off')

        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)

        self.reload_settings()

        #cleanup GPIO
        self.cleanup_gpio()

        #configure GPIO
        if self.config['switchingMethod'] == 'GPIO' or self.config['sensingMethod'] == 'GPIO':
            self.configure_gpio()

        self._start_idle_timer()


    def get_wizard_version(self):
        """Return the setup-wizard version."""
        return 1


    def is_wizard_required(self):
        """Report whether the first-run setup wizard should be shown."""
        return True


    def get_settings_version(self):
        """Return the settings schema version."""
        return 4


    def on_settings_migrate(self, target, current=None):
        """Upgrade stored settings from older schema versions to the current one."""
        if current is None:
            current = 0

        if current < 2:
            # v2 changes names of settings variables to accomidate system commands.
            cur_switchingMethod = self._settings.get(["switchingMethod"])
            if cur_switchingMethod is not None and cur_switchingMethod == "COMMAND":
                self._logger.info("Migrating Setting: switchingMethod=COMMAND -> switchingMethod=GCODE")
                self._settings.set(["switchingMethod"], "GCODE")

            cur_onCommand = self._settings.get(["onCommand"])
            if cur_onCommand is not None:
                self._logger.info("Migrating Setting: onCommand={0} -> onGCodeCommand={0}".format(cur_onCommand))
                self._settings.set(["onGCodeCommand"], cur_onCommand)
                self._settings.remove(["onCommand"])
            
            cur_offCommand = self._settings.get(["offCommand"])
            if cur_offCommand is not None:
                self._logger.info("Migrating Setting: offCommand={0} -> offGCodeCommand={0}".format(cur_offCommand))
                self._settings.set(["offGCodeCommand"], cur_offCommand)
                self._settings.remove(["offCommand"])

            cur_autoOnCommands = self._settings.get(["autoOnCommands"])
            if cur_autoOnCommands is not None:
                self._logger.info("Migrating Setting: autoOnCommands={0} -> autoOnTriggerGCodeCommands={0}".format(cur_autoOnCommands))
                self._settings.set(["autoOnTriggerGCodeCommands"], cur_autoOnCommands)
                self._settings.remove(["autoOnCommands"])

        if current < 3:
            # v3 adds support for multiple sensing methods
            cur_enableSensing = self._settings.get_boolean(["enableSensing"])
            if cur_enableSensing is not None and cur_enableSensing:
                self._logger.info("Migrating Setting: enableSensing=True -> sensingMethod=GPIO")
                self._settings.set(["sensingMethod"], "GPIO")
                self._settings.remove(["enableSensing"])

        if current < 4:
            # v4 drops RPi.GPIO in favor of Python-Periphery.
            cur_GPIOMode = self._settings.get(["GPIOMode"])
            cur_switchingMethod = self._settings.get(["switchingMethod"])
            cur_sensingMethod = self._settings.get(["sensingMethod"])
            cur_onoffGPIOPin = self._settings.get_int(["onoffGPIOPin"])
            cur_invertonoffGPIOPin = self._settings.get_boolean(["invertonoffGPIOPin"])
            cur_senseGPIOPin = self._settings.get_int(["senseGPIOPin"])
            cur_invertsenseGPIOPin = self._settings.get_boolean(["invertsenseGPIOPin"])
            cur_senseGPIOPinPUD = self._settings.get(["senseGPIOPinPUD"])

            if cur_switchingMethod == 'GPIO' or cur_sensingMethod == 'GPIO':
                if cur_GPIOMode == 'BOARD':
                    # Convert BOARD pin numbers to BCM

                    def _gpio_board_to_bcm(pin):
                        _pin_to_gpio_rev1 = [-1, -1, -1, 0, -1, 1, -1, 4, 14, -1, 15, 17, 18, 21, -1, 22, 23, -1, 24, 10, -1, 9, 25, 11, 8, -1, 7, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1 ]
                        _pin_to_gpio_rev2 = [-1, -1, -1, 2, -1, 3, -1, 4, 14, -1, 15, 17, 18, 27, -1, 22, 23, -1, 24, 10, -1, 9, 25, 11, 8, -1, 7, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1 ]
                        _pin_to_gpio_rev3 = [-1, -1, -1, 2, -1, 3, -1, 4, 14, -1, 15, 17, 18, 27, -1, 22, 23, -1, 24, 10, -1, 9, 25, 11, 8, -1, 7, -1, -1, 5, -1, 6, 12, 13, -1, 19, 16, 26, 20, -1, 21 ]

                        if GPIO.RPI_REVISION == 1:
                            pin_to_gpio = _pin_to_gpio_rev1
                        elif GPIO.RPI_REVISION == 2:
                            pin_to_gpio = _pin_to_gpio_rev2
                        else:
                            pin_to_gpio = _pin_to_gpio_rev3

                        return pin_to_gpio[pin]

                    try:
                        import RPi.GPIO as GPIO
                        _has_gpio = True
                    except (ImportError, RuntimeError):
                        self._logger.exception("Error importing RPi.GPIO. BOARD->BCM conversion will not occur")
                        _has_gpio = False

                    if cur_switchingMethod == 'GPIO' and _has_gpio:
                        p = _gpio_board_to_bcm(cur_onoffGPIOPin)
                        self._logger.info("Converting pin number from BOARD to BCM. onoffGPIOPin={} -> onoffGPIOPin={}".format(cur_onoffGPIOPin, p))
                        self._settings.set_int(["onoffGPIOPin"], p)

                    if cur_sensingMethod == 'GPIO' and _has_gpio:
                        p = _gpio_board_to_bcm(cur_senseGPIOPin)
                        self._logger.info("Converting pin number from BOARD to BCM. senseGPIOPin={} -> senseGPIOPin={}".format(cur_senseGPIOPin, p))
                        self._settings.set_int(["senseGPIOPin"], p)

                if len(self._availableGPIODevices) > 0:
                    # This was likely a Raspberry Pi using RPi.GPIO. Set GPIODevice to the first dev found which is likely /dev/gpiochip0
                    self._logger.info("Setting GPIODevice to the first found. GPIODevice={}".format(self._availableGPIODevices[0]))
                    self._settings.set(["GPIODevice"], self._availableGPIODevices[0])
                else:
                    # GPIO was used for either but no GPIO devices exist. Reset to defaults.
                    self._logger.warning("No GPIO devices found. Reverting switchingMethod and sensingMethod to defaults.")
                    self._settings.remove(["switchingMethod"])
                    self._settings.remove(["sensingMethod"])


                # Write the config to lightcontrol_rpigpio just in case the user decides/needs to switch to it.
                self._logger.info("Writing original GPIO related settings to lightcontrol_rpigpio.")
                self._settings.global_set(['plugins', 'lightcontrol_rpigpio', 'GPIOMode'], cur_GPIOMode)
                self._settings.global_set(['plugins', 'lightcontrol_rpigpio', 'switchingMethod'], cur_switchingMethod)
                self._settings.global_set(['plugins', 'lightcontrol_rpigpio', 'sensingMethod'], cur_sensingMethod)
                self._settings.global_set_int(['plugins', 'lightcontrol_rpigpio', 'onoffGPIOPin'], cur_onoffGPIOPin)
                self._settings.global_set_boolean(['plugins', 'lightcontrol_rpigpio', 'invertonoffGPIOPin'], cur_invertonoffGPIOPin)
                self._settings.global_set_int(['plugins', 'lightcontrol_rpigpio', 'senseGPIOPin'], cur_senseGPIOPin)
                self._settings.global_set_boolean(['plugins', 'lightcontrol_rpigpio', 'invertsenseGPIOPin'], cur_invertsenseGPIOPin)
                self._settings.global_set(['plugins', 'lightcontrol_rpigpio', 'senseGPIOPinPUD'], cur_senseGPIOPinPUD)
            else:
                self._logger.info("No GPIO pins to convert.")

            # Remove now unused config option
            self._logger.info("Removing Setting: GPIOMode")
            self._settings.remove(["GPIOMode"])


    def get_template_vars(self):
        """Expose GPIO devices, registered sub-plugins, and capability flags to the templates."""
        available_plugins = []
        for k in list(self._sub_plugins.keys()):
            available_plugins.append(dict(pluginIdentifier=k, displayName=self._plugin_manager.plugins[k].name))

        return {
            "availableGPIODevices": self._availableGPIODevices,
            "availablePlugins": available_plugins,
            "hasGPIO": HAS_GPIO,
            "supportsLineBias": SUPPORTS_LINE_BIAS
        }


    def is_template_autoescaped(self):
        """Enable Jinja autoescaping for the plugin's templates."""
        return True


    def get_template_configs(self):
        """Declare the settings template with custom Knockout bindings."""
        return [
            dict(type="settings", custom_bindings=True)
        ]


    def get_assets(self):
        """Declare the plugin's static JavaScript, LESS, and CSS assets."""
        return {
            "js": ["js/lightcontrol.js"],
            "less": ["less/lightcontrol.less"],
            "css": ["css/lightcontrol.min.css"]
        } 


    def get_update_information(self):
        """Provide Software Update plugin metadata for GitHub release checks."""
        return dict(
            lightcontrol=dict(
                displayName="Light Control",
                displayVersion=self._plugin_version,

                # version check: github repository
                type="github_release",
                user="arbeaman",
                repo="OctoPrint-LightControl",
                current=self._plugin_version,

                # update method: pip w/ dependency links
                pip="https://github.com/arbeaman/OctoPrint-LightControl/archive/{target_version}.zip"
            )
        )


    def register_custom_events(self):
        """Register the state-changed event fired when the output turns on or off."""
        return ["light_state_changed"]


    def get_additional_permissions(self, *args, **kwargs):
        """Define the permission that guards switching the output on and off."""
        return [
            dict(key="CONTROL",
                 name="Control",
                 description=gettext("Allows switching Light on/off"),
                 roles=["admin"],
                 dangerous=True,
                 default_groups=[Permissions.ADMIN_GROUP])
        ]


    def _hook_octoprint_server_api_before_request(self, *args, **kwargs):
        """Register the pre-request check that powers the lights on for print-on-upload requests."""
        return [self.turn_on_before_printing_after_upload]


__plugin_name__ = "Light Control"
__plugin_pythoncompat__ = ">=3,<4"

def __plugin_load__():
    """Instantiate the plugin and register its hooks and helpers."""
    global __plugin_implementation__
    __plugin_implementation__ = LightControl()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.comm.protocol.gcode.queuing": __plugin_implementation__.hook_gcode_queuing,
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
        "octoprint.events.register_custom_events": __plugin_implementation__.register_custom_events,
        "octoprint.access.permissions": __plugin_implementation__.get_additional_permissions,
        "octoprint.cli.commands": cli.commands,
        "octoprint.server.api.before_request": __plugin_implementation__._hook_octoprint_server_api_before_request,
    }

    global __plugin_helpers__
    __plugin_helpers__ = dict(
        get_light_state = __plugin_implementation__.get_light_state,
        turn_light_on = __plugin_implementation__.turn_light_on,
        turn_light_off = __plugin_implementation__.turn_light_off,
        register_plugin = __plugin_implementation__.register_plugin
    )

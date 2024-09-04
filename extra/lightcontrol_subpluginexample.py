# coding=utf-8
from __future__ import absolute_import

__author__ = "Rick Beaman <35195829+arbeaman@users.noreply.github.com>"
__license__ = "GNU Affero General Public License http://www.gnu.org/licenses/agpl.html"
__copyright__ = "Copyright (C) 2021 Rick Beaman - Released under terms of the AGPLv3 License"

import octoprint.plugin

class LightControl_SubPluginExample(octoprint.plugin.StartupPlugin,
                                  octoprint.plugin.RestartNeedingPlugin):

    def __init__(self):
        self.status = False


    def on_startup(self, host, port):
        lightcontrol_helpers = self._plugin_manager.get_helpers("lightcontrol")
        if not lightcontrol_helpers or 'register_plugin' not in lightcontrol_helpers.keys():
            self._logger.warning("The version of LightControl that is installed does not support plugin registration.")
            return

        self._logger.debug("Registering plugin with LightControl")
        lightcontrol_helpers['register_plugin'](self)


    def turn_light_on(self):
        self._logger.info("ON")
        self.status = True


    def turn_light_off(self):
        self._logger.info("OFF")
        self.status = False


    def get_light_state(self):
        return self.status


__plugin_name__ = "Light Control - Sub Plugin Example"
__plugin_pythoncompat__ = ">=3.0,<4"

def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = LightControl_SubPluginExample()

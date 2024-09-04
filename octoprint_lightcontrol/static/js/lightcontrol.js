$(function() {
    function LightControlViewModel(parameters) {
        var self = this;

        self.settingsViewModel = parameters[0]
        self.loginState = parameters[1];
        
        self.settings = undefined;

        self.sensingPlugin_old = "";
        self.switchingPlugin_old = "";

        self.scripts_gcode_lightcontrol_post_on = ko.observable(undefined);
        self.scripts_gcode_lightcontrol_pre_off = ko.observable(undefined);

        self.isLightOn = ko.observable(undefined);

        self.light_indicator = $("#lightcontrol_indicator");

        self.onBeforeBinding = function() {
            self.settings = self.settingsViewModel.settings;

            self.settings.plugins.lightcontrol.sensingPlugin.subscribe(function(oldValue) {
                self.sensingPlugin_old = oldValue;
            }, this, 'beforeChange');

            self.settings.plugins.lightcontrol.switchingPlugin.subscribe(function(oldValue) {
                self.switchingPlugin_old = oldValue;
            }, this, 'beforeChange');

            self.settings.plugins.lightcontrol.sensingPlugin.subscribe(function(newValue) {
                if (newValue === "_GET_MORE_") {
                    self.openGetMore();
                    self.settings.plugins.lightcontrol.sensingPlugin(self.sensingPlugin_old);
                }
            });

            self.settings.plugins.lightcontrol.switchingPlugin.subscribe(function(newValue) {
                if (newValue === "_GET_MORE_") {
                    self.openGetMore();
                    self.settings.plugins.lightcontrol.switchingPlugin(self.switchingPlugin_old);
                }
            });

            self.sensingPlugin_old = self.settings.plugins.lightcontrol.sensingPlugin();
            self.switchingPlugin_old = self.settings.plugins.lightcontrol.switchingPlugin();
        };

        self.onSettingsShown = function () {
            self.scripts_gcode_lightcontrol_post_on(self.settings.scripts.gcode["lightcontrol_post_on"]());
            self.scripts_gcode_lightcontrol_pre_off(self.settings.scripts.gcode["lightcontrol_pre_off"]());
        };

        self.onSettingsHidden = function () {
            self.settings.plugins.lightcontrol.scripts_gcode_lightcontrol_post_on = null;
            self.settings.plugins.lightcontrol.scripts_gcode_lightcontrol_pre_off = null;
        };

        self.onSettingsBeforeSave = function () {
            if (self.scripts_gcode_lightcontrol_post_on() !== undefined) {
                if (self.scripts_gcode_lightcontrol_post_on() != self.settings.scripts.gcode["lightcontrol_post_on"]()) {
                    self.settings.plugins.lightcontrol.scripts_gcode_lightcontrol_post_on = self.scripts_gcode_lightcontrol_post_on;
                    self.settings.scripts.gcode["lightcontrol_post_on"](self.scripts_gcode_lightcontrol_post_on());
                }
            }

            if (self.scripts_gcode_lightcontrol_pre_off() !== undefined) {
                if (self.scripts_gcode_lightcontrol_pre_off() != self.settings.scripts.gcode["lightcontrol_pre_off"]()) {
                    self.settings.plugins.lightcontrol.scripts_gcode_lightcontrol_pre_off = self.scripts_gcode_lightcontrol_pre_off;
                    self.settings.scripts.gcode["lightcontrol_pre_off"](self.scripts_gcode_lightcontrol_pre_off());
                }
            }
        };

        self.onStartup = function () {
            self.isLightOn.subscribe(function() {
                if (self.isLightOn()) {
                    self.light_indicator.removeClass("off").addClass("on");
                } else {
                    self.light_indicator.removeClass("on").addClass("off");
                }   
            });

            $.ajax({
                url: API_BASEURL + "plugin/lightcontrol",
                type: "POST",
                dataType: "json",
                data: JSON.stringify({
                    command: "getLightState"
                }),
                contentType: "application/json; charset=UTF-8"
            }).done(function(data) {
                self.isLightOn(data.isLightOn);
            });
        }

        self.onDataUpdaterPluginMessage = function(plugin, data) {
            if (plugin != "lightcontrol") {
                return;
            }

            if (data.isLightOn !== undefined) {
                self.isLightOn(data.isLightOn);
            }
        };

        self.toggleLight = function() {
            if (self.isLightOn()) {
                if (self.settings.plugins.lightcontrol.enablePowerOffWarningDialog()) {
                    showConfirmationDialog({
                        message: "You are about to turn off the Light.",
                        onproceed: function() {
                            self.turnLightOff();
                        }
                    });
                } else {
                    self.turnLightOff();
                }
            } else {
                self.turnLightOn();
            }
        };

        self.turnLightOn = function() {
            $.ajax({
                url: API_BASEURL + "plugin/lightcontrol",
                type: "POST",
                dataType: "json",
                data: JSON.stringify({
                    command: "turnLightOn"
                }),
                contentType: "application/json; charset=UTF-8"
            })
        };

    	self.turnLightOff = function() {
            $.ajax({
                url: API_BASEURL + "plugin/lightcontrol",
                type: "POST",
                dataType: "json",
                data: JSON.stringify({
                    command: "turnLightOff"
                }),
                contentType: "application/json; charset=UTF-8"
            })
        };

        self.subPluginTabExists = function(id) {
            return $('#settings_plugin_' + id).length > 0
        };

        self.openGetMore = function() {
            window.open("https://plugins.octoprint.org/by_tag/#tag-lightcontrol-subplugin", "_blank");
        };
    }

    ADDITIONAL_VIEWMODELS.push([
        LightControlViewModel,
        ["settingsViewModel", "loginStateViewModel"],
        ["#navbar_plugin_lightcontrol", "#settings_plugin_lightcontrol"]
    ]);
});

# coding=utf-8

__author__ = "Rick Beaman <35195829+arbeaman@users.noreply.github.com>"
__license__ = "GNU Affero General Public License http://www.gnu.org/licenses/agpl.html"
__copyright__ = "Copyright (C) 2024 Rick Beaman - Released under terms of the AGPLv3 License"

def commands(cli_group, pass_octoprint_ctx, *args, **kwargs):
    # Requires OctoPrint >= 1.3.5
    import click
    import sys
    import json
    import requests.exceptions
    from octoprint.cli.client import create_client, client_options

    def _api_command(command, apikey, host, port, httpuser, httppass, https, prefix):
        if prefix == None:
            prefix = '/api'

        client = create_client(settings=cli_group.settings,
                               apikey=apikey,
                               host=host,
                               port=port,
                               httpuser=httpuser,
                               httppass=httppass,
                               https=https,
                               prefix=prefix)

        r = client.post_command("plugin/ightcontrol", command)
        try:
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            click.echo("HTTP Error, got {}".format(e))
            sys.exit(1)

        return r

    @client_options
    @click.command("on")
    def turnLightOn_command(apikey, host, port, httpuser, httppass, https, prefix):
        """Turn the Light On"""
        r = _api_command('turnLightOn', apikey, host, port, httpuser, httppass, https, prefix)

        if r.status_code in [200, 204]:
            click.echo('ok')

    @client_options
    @click.command("off")
    def turnLightOff_command(apikey, host, port, httpuser, httppass, https, prefix):
        """Turn the Light Off"""
        r = _api_command('turnLightOff', apikey, host, port, httpuser, httppass, https, prefix)

        if r.status_code in [200, 204]:
            click.echo('ok')

    @client_options
    @click.command("toggle")
    def toggleLight_command(apikey, host, port, httpuser, httppass, https, prefix):
        """Toggle the Light On/Off"""
        r = _api_command('toggleLight', apikey, host, port, httpuser, httppass, https, prefix)

        if r.status_code in [200, 204]:
            click.echo('ok')

    @click.option("--return-int", is_flag=True, help="Return the Light state as a boolean integer.")
    @client_options
    @click.command("status")
    def getLightState_command(return_int, apikey, host, port, httpuser, httppass, https, prefix):
        """Get the current Light status"""
        r = _api_command('getLightState', apikey, host, port, httpuser, httppass, https, prefix)

        if r.status_code in [200, 204]:
            data = json.loads(r._content)

            if return_int:
                click.echo(int(data['isLightOn']))
            else:
                if data['isLightOn']:
                    click.echo('on')
                else:
                    click.echo('off')

    return [turnLightOn_command, turnLightOff_command, toggleLight_command, getLightState_command]


# Bonaire My Climate Custom Component

This Home Assistant custom component is a reverse engineered implementation of the "My Climate" iOS/Android app which is used to control the Wi-Fi module of a Bonaire heating and/or cooling system.
This is in no way associated with the company Bonaire and comes with no guarantees or warranty. Use at your own risk.

## Installation

[![hacs][hacsbadge]][hacs]

Install via HACS.

## Prerequisites

- You must have the Bonaire Wi-Fi Module installed and connected to your Wi-Fi.
- You must not have your iOS/Android app connected to the module.

## Configuration

After you have installed the custom component (see above):
1. Goto the `Configuration` -> `Integrations` page.  
2. On the bottom right of the page, click on the `+ Add Integration` sign to add an integration.
3. Search for `Bonaire MyClimate`. (If you don't see it, try refreshing your browser page to reload the cache.)
4. Either enter the module's IP address, or leave it blank and click `Submit` to scan your network for it (it will ask you to confirm what it finds).

### Finding the module / changing its IP address

The module is normally found by a UDP broadcast, but on many Wi-Fi networks the module misses broadcasts (it answers a message sent straight to its own IP immediately). This version therefore:

- sends discovery directly to the module's known IP address (the one you entered, or the one it last connected from) as well as broadcasting,
- scans every address on the subnet if the module still cannot be found, for example because its IP address changed, and remembers the new address,
- lets you change the IP address later: `Settings` -> `Devices & services` -> `Bonaire MyClimate` -> `Configure`. Leave it blank to find the module automatically.

Giving the module a fixed address (a DHCP reservation on your router) is still the most reliable setup.

## Development

The discovery code has no Home Assistant dependency and is tested against a simulated module (Linux/macOS):
`python3 -m unittest discover -s tests -v`

## Troubleshooting

Please set your logging for the custom_component to debug:
```yaml
logger:
  default: warn
  logs:
    custom_components.bonaire_myclimate: debug
```

## Using "Simple Thermostat"

There is a custom card that looks great and works really well with this climate component. https://github.com/nervetattoo/simple-thermostat
This is the lovelace code I use to display my thermostat.

![image](https://user-images.githubusercontent.com/34525505/123367525-6c73dd80-d5bd-11eb-8efb-229a32e3e9aa.png)

```yaml
type: custom:simple-thermostat
entity: climate.bonaire_myclimate
header: false
layout:
  step: row
  mode:
    headings: false
hide:
  state: true
decimals: '0'
step_size: '1'
control:
  _headings: false
  hvac:
    cool:
      name: Cool
    fan_only:
      name: Fan Only
    heat:
      name: Heat
    'off':
      name: 'Off'
  preset:
    '1':
      name: Downstairs
    '2':
      name: Upstairs
    _hide_when_off: true
    1,2:
      name: Everywhere
```
<a href="https://www.buymeacoffee.com/bremor" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height=40px width=144px></a>

[hacs]: https://hacs.xyz
[hacsbadge]: https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge

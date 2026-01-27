## IKEA Dirigera Hub Integration

This is an actively maintained fork of [sanjoyg/dirigera_platform](https://github.com/sanjoyg/dirigera_platform), which appears to be abandoned (last activity March 2025). This integration connects Home Assistant with the IKEA Dirigera hub, built on the [dirigera](https://github.com/Leggin/dirigera) Python library by Nicolas Hilberg.

This fork addresses most of the outstanding issues from the upstream repository. Contributions are welcome — feel free to open [issues](https://github.com/nrbrt/dirigera_platform/issues) or submit pull requests.

Should the upstream repository become active again, I reccommend reverting back. In the meantime, I will try to answer questions and update things on both my fork and the upstream repository, while filing PRs upstream of all changes I make, just in case the upstream maintainer resurfaces and wants to continue working on it.

### Supported Devices
* Lights (including RGBWW with dynamic color mode switching)
* Outlets (with energy monitoring)
* Open/Close Sensors (PARASOLL, MYGGBETT)
* Motion Sensors (VALLHORN, MYGGSPRAY)
* Environment Sensors (VINDSTYRKA, ALPSTUGA including CO2)
* FYRTUR/KADRILJ Blinds
* STYRBAR / RODRET / SOMRIG Remotes - with automation events
* AirPurifier / STARKVIND
* Water Leak Sensors (BADRING)
* Scenes

### What this fork adds

**Dynamic Device Discovery** ([#139](https://github.com/sanjoyg/dirigera_platform/issues/139))
- Devices added to or removed from the Dirigera hub are automatically reflected in Home Assistant — no restart required
- Name and room changes made in the IKEA Home app sync to HA in real-time

**MYGGSPRAY Motion Sensors** (E2494)
- IKEA reports these as `occupancySensor` instead of `motionSensor` — this fork handles both types
- Full WebSocket event support for real-time motion detection

**Light Color Mode Switching**
- Fixes color state not updating when changed via the IKEA Home app
- RGBWW lamps now correctly switch between HS color and color temperature modes
- Adds `colorHue` and `colorSaturation` to WebSocket event processing

**RODRET / STYRBAR Remote Support**
- Adds `remotePressEvent` handling for `lightController` devices
- STYRBAR (E2002) mapped with all 4 buttons
- Fixes device trigger prefix mismatch that broke automations

**Additional Fixes**
- Device reachability: devices now correctly show as unavailable when offline ([#147](https://github.com/sanjoyg/dirigera_platform/issues/147))
- Color temperature: fixes mired/Kelvin unit conversion
- ALPSTUGA: adds CO2 sensor support
- Environment sensors: adds `state_class: measurement` for Long Term Statistics
- STARKVIND: fixes native unit of measurement
- Outlet power sensor: marked as measurement for energy dashboard
- Deprecation warning fixed for HA 2024.12+ (`OptionsFlowWithConfigEntry`)


## Pre-requisite
1. Identify the IP of the gateway - Usually looking at the client list in your home router interface will give that.

## Installing

### From this fork (recommended)
- In Home Assistant, go to **HACS** → **Integrations** → **⋮** (top right) → **Custom repositories**
- Add `https://github.com/nrbrt/dirigera_platform` as an **Integration**
- Search for "Dirigera" and install

### From upstream (may be outdated)
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=sanjoyg&repository=dirigera_platform&category=integration)

## Using the integration
1. One you get to add integration and get to the configuration screen, the IP of the gateway will be requested. 
   **IMPORTANT**
   Before hitting enter be near the IKEA Dirigera hub as post entering IP a request to press the action button on the hub

2. Once you get the screen requesting to press the action button, physically press the button once and then click on submit

3. If the IP is right and action button has been pressed, then the integration will be added and all devices registered will be shown. At this time the following device types are supported
    
   In addition you'll find the scenes added as individual entities. Go to the "Entities" to find them as they're not part of any device. Use the "Activate" button to trigger a scene.

## Testing installation with mock
1. If you enter the IP as "mock" then mock bulbs and outlet will be added.
2. Once you verify that the bulbs and outlets are added feel free to delete the integration

Here is how it looks

1. After you have downloaded the integration from HACS and go to Setting -> Integration -> ADD INTEGRATION to add the dirigera integration, the following screen will come up

![](screenshots/config-ip-details.png)

To test the integration, enter the IP as "mock". The check-box indicates if the bulbs/lights associated with a device-set should be visible as entities or not

![](screenshots/config-mock.png)

The integration would prompt to press the action button on the hub

![](screenshots/config-press-action.png)

Since this is mock, we would get a success message

![](screenshots/config-hub-setup-complete-mock.png)

Once this is complete you would see two bulbs and two outlets appearing.

![](screenshots/mock-lights.png)
![](screenshots/mock-outlets.png)

## Raising Issue

Now I dont have access to all sensors, hence what will be useful is when you raise an issue also supply to the JSON that the hub returns.
To get the JSON do the following

* Go to Developer -> Service and invoke dirigera_platform.dump_data without any parameters
* Look at the HASS log which would have the JSON. 
* If you see any platform errors include that as well

[Detailed Instructions](docs/dump-data.md)



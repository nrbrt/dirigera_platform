# Calling dump_data to dump the JSON

Instructions to dump JSON from the hub, helping new device addition and debugging existing issues.

> Since 0.4.1 the easier way is **Download diagnostics** on the integration page (see the README). It contains the same anonymised device list, without going through the log.

### Step 1 - Open Settings and click on the Integrations link

![](../screenshots/settings-deviceandservices.png)

### Step 2 - Locate the Dirigera Integration and click on it

![](../screenshots/dirigera-integration-locate.png)

### Step 3 - Click on "Enable Debugging"

![](../screenshots/enable-debugging.png)

### Step 4 - Click on "Developer Tools" from the sidebar, then "Services" from the top nav bar

![](../screenshots/developertools-services.png)

### Step 5 - Type the service name "dump_data" and click "Call Service"

![](../screenshots/calling-dump-data.png)

### Step 6 - Click on "Settings" in the sidebar, then click "System"

![](../screenshots/settings-system.png)

### Step 7 - Click on "Logs", locate the JSON dump and copy everything between "START JSON" and "END JSON" to paste in your issue

![](../screenshots/system-logs.png)

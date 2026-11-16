# Test image for the real-Home-Assistant tests (pintest). Not used by the integration itself.
# pytest-homeassistant-custom-component pins an exact homeassistant release; the HA image
# provides Python and most compiled dependencies, pip then aligns homeassistant to the pin.
FROM ghcr.io/home-assistant/home-assistant:2026.11.1
RUN pip install --no-cache-dir pytest-homeassistant-custom-component==0.13.375 dirigera==1.6.1
WORKDIR /repo
ENTRYPOINT ["python", "-m", "pytest"]

# Lightrover Web Teleop Azure Sample Setup

This guide deploys the web UI and FastAPI BFF to Azure App Service, then connects it to the factory Edge PC through a Site-to-Site VPN.

## 1. Assumptions

Replace these values before running the commands.

| Name | Example |
|---|---|
| Resource group | `rg-lightrover-sample` |
| Azure region | `japaneast` |
| App Service name | `app-lightrover-sample` |
| Azure VNet CIDR | `10.10.0.0/16` |
| App Service subnet | `10.10.1.0/24` |
| Gateway subnet | `10.10.255.0/27` |
| Factory LAN CIDR | `192.168.100.0/24` |
| Factory Edge PC | `192.168.100.10` |
| Factory VPN public IP | `xxx.xxx.xxx.xxx` |
| VPN shared key | `replace-with-a-long-shared-key` |

## 2. Create Azure Network

```bash
az group create \
  --name rg-lightrover-sample \
  --location japaneast

az network vnet create \
  --resource-group rg-lightrover-sample \
  --name vnet-lightrover-sample \
  --address-prefix 10.10.0.0/16 \
  --subnet-name subnet-appservice \
  --subnet-prefix 10.10.1.0/24

az network vnet subnet create \
  --resource-group rg-lightrover-sample \
  --vnet-name vnet-lightrover-sample \
  --name GatewaySubnet \
  --address-prefix 10.10.255.0/27
```

## 3. Create VPN Gateway

```bash
az network public-ip create \
  --resource-group rg-lightrover-sample \
  --name pip-vpngw-lightrover-sample \
  --allocation-method Static \
  --sku Standard

az network vnet-gateway create \
  --resource-group rg-lightrover-sample \
  --name vpngw-lightrover-sample \
  --public-ip-addresses pip-vpngw-lightrover-sample \
  --vnet vnet-lightrover-sample \
  --gateway-type Vpn \
  --vpn-type RouteBased \
  --sku VpnGw1
```

Gateway creation can take 30-45 minutes.

Get the Azure VPN public IP after creation:

```bash
az network public-ip show \
  --resource-group rg-lightrover-sample \
  --name pip-vpngw-lightrover-sample \
  --query ipAddress \
  --output tsv
```

## 4. Create Factory VPN Connection

```bash
az network local-gateway create \
  --resource-group rg-lightrover-sample \
  --name lngw-factory-sample \
  --gateway-ip-address xxx.xxx.xxx.xxx \
  --local-address-prefixes 192.168.100.0/24

az network vpn-connection create \
  --resource-group rg-lightrover-sample \
  --name conn-azure-factory-sample \
  --vnet-gateway1 vpngw-lightrover-sample \
  --local-gateway2 lngw-factory-sample \
  --shared-key "replace-with-a-long-shared-key"
```

On the factory VPN router, configure Site-to-Site IPsec/IKEv2 with:

| Item | Value |
|---|---|
| Peer public IP | Azure VPN Gateway public IP |
| Shared key | Same value used in `az network vpn-connection create` |
| Azure CIDR | `10.10.0.0/16` |
| Factory CIDR | `192.168.100.0/24` |
| Routing | Send Azure VNet traffic to the VPN tunnel |

## 5. Create App Service

```bash
az appservice plan create \
  --resource-group rg-lightrover-sample \
  --name plan-lightrover-sample \
  --is-linux \
  --sku B1

az webapp create \
  --resource-group rg-lightrover-sample \
  --plan plan-lightrover-sample \
  --name app-lightrover-sample \
  --runtime "PYTHON:3.12"

az webapp vnet-integration add \
  --resource-group rg-lightrover-sample \
  --name app-lightrover-sample \
  --vnet vnet-lightrover-sample \
  --subnet subnet-appservice
```

## 6. Configure App Service

```bash
az webapp config set \
  --resource-group rg-lightrover-sample \
  --name app-lightrover-sample \
  --startup-file "uvicorn server.run_server:app --host 0.0.0.0 --port 8000"

az webapp config appsettings set \
  --resource-group rg-lightrover-sample \
  --name app-lightrover-sample \
  --settings \
    APP_MODE=azure_bff \
    FACTORY_EDGE_BASE_URL=http://192.168.100.10:8001 \
    FACTORY_EDGE_TIMEOUT_SEC=3.0 \
    FACTORY_EDGE_POLL_SEC=0.5 \
    SKYWAY_APP_ID=xxxxxxxx \
    SKYWAY_SECRET_KEY=xxxxxxxx \
    SKYWAY_ROOM_PREFIX=lightrover \
    SCM_DO_BUILD_DURING_DEPLOYMENT=true
```

## 7. Deploy App Code

From the repository root:

```bash
zip -r lightrover-web-azure.zip \
  server web config maps requirements.txt factory_edge_gateway.py

az webapp deploy \
  --resource-group rg-lightrover-sample \
  --name app-lightrover-sample \
  --src-path lightrover-web-azure.zip \
  --type zip
```

## 8. Start Factory Edge Gateway

Run this on the factory Edge PC:

```bash
cd ~/lightrover_web_teleop_skyway
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

source /opt/ros/jazzy/setup.bash
source ~/ros2_ws/install/setup.bash
export ROS_DOMAIN_ID=1
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CMD_VEL_TOPIC=/rover_twist_cmd
export WATCHDOG_SERVICE_NAME=/lightrover_safety_watchdog/set_parameters

uvicorn factory_edge_gateway:app --host 0.0.0.0 --port 8001
```

For a quick API-only health check before ROS is ready:

```bash
curl http://localhost:8001/health
```

The command API requires ROS packages to be sourced and should return HTTP 503 if ROS is unavailable.

## 9. Verify Connectivity

From App Service SSH or Kudu:

```bash
curl http://192.168.100.10:8001/health
```

Expected:

```json
{"status":"ok","ros_available":true,"ros_error":null}
```

From your local machine:

```bash
curl https://app-lightrover-sample.azurewebsites.net/api/health

curl -X POST https://app-lightrover-sample.azurewebsites.net/api/robots/lightrover1/cmd_vel \
  -H "Content-Type: application/json" \
  -d '{"linear_x":0.05,"angular_z":0.0,"duration_ms":200}'

curl -X POST https://app-lightrover-sample.azurewebsites.net/api/robots/lightrover1/stop
```

Open:

```text
https://app-lightrover-sample.azurewebsites.net/
```

Check that:

- The robot list appears.
- WebSocket state is connected.
- Stop works.
- Short forward/back/turn commands move the rover only while the button is held.
- SkyWay token requests succeed after `SKYWAY_APP_ID` and `SKYWAY_SECRET_KEY` are set.

## 10. Useful Runtime Modes

Local PC-heavy mode:

```bash
APP_MODE=local uvicorn server.run_server:app --host 0.0.0.0 --port 8080
```

Azure BFF mode:

```bash
APP_MODE=azure_bff \
FACTORY_EDGE_BASE_URL=http://192.168.100.10:8001 \
uvicorn server.run_server:app --host 0.0.0.0 --port 8000
```

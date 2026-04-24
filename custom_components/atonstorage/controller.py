"""AtonStorage controller"""
import json
import logging
import re
from datetime import datetime

import httpx
from homeassistant.core import HomeAssistant
from homeassistant.helpers.httpx_client import get_async_client

_BASEURL = "https://www.atonstorage.com/atonTC/"
_LOGIN_ENDPOINT = _BASEURL + "index.php"
_MONITOR_ENDPOINT = _BASEURL + "get_monitor.php?sn={serial_number}"
_ENERGY_ENDPOINT = (
    _BASEURL
    + "get_energy.php?idImpianto={id}&anno={year}&mese={month}&giorno={day}&intervallo=d"
)  # tot_pReteOut
_SET_REQUEST_ENDPOINT = (
    _BASEURL
    + "set_request.php?request=MONITOR&intervallo={interval}&sn={serial_number}"
)

_LOGGER = logging.getLogger(__name__)


class Controller:
    """Define a generic AtonStorage sensor."""

    def __init__(self, hass: HomeAssistant, user, password, serial_number, opts):
        """Initialize."""

        if serial_number is None:
            raise SerialNumberRequiredError

        self._hass = hass
        self._user = user
        self._password = password
        self._serial_number = serial_number
        self._opts = opts
        self._session = None
        self._id_plant = None
        self.data = {}
        self._async_client = get_async_client(hass, verify_ssl=False)

    async def login(self) -> bool:
        """Login to Aton server."""
        try:
            _LOGGER.debug("Logging in to Aton server")
            login_page = await self._async_client.get(_LOGIN_ENDPOINT, timeout=30)
            login_page.raise_for_status()

            login_resp = await self._async_client.post(
                _LOGIN_ENDPOINT,
                timeout=30,
                data={
                    "username": self._user,
                    "password": self._password,
                },
                cookies=login_page.cookies,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            login_resp.raise_for_status()

            if login_resp.headers.get("Set-Cookie"):
                self._session = login_resp.cookies
                _LOGGER.info("Logged in successfully")

                # get plant id
                p = re.compile(r"var idImpianto = (.*);")
                result = p.search(login_resp.text)
                if result:
                    self._id_plant = result.group(1).strip().strip("'").strip('"')
                    _LOGGER.info("idImpianto=%s", self._id_plant)
                    return True
                else:
                    _LOGGER.error("Could not find idImpianto in login response")
            else:
                _LOGGER.error("Login failed: No Set-Cookie header received")
        except httpx.HTTPError as exc:
            _LOGGER.error("HTTP error during login: %s", exc)
        except Exception as exc:
            _LOGGER.error("Unexpected error during login: %s", exc)

        return False

    def _check_response(self, response: httpx.Response):
        """Check if response is authorized and successful."""
        response.raise_for_status()
        if response.text == "Unauthorized":
            _LOGGER.warning("Session unauthorized, clearing session")
            self._session = None
            raise AtonStorageConnectionError("Unauthorized")

    async def refresh(self) -> None:
        """Refresh data from server"""

        if self._session is None:
            if not await self.login():
                raise InvalidUsernameOrPasswordError

        try:
            # Set refresh interval
            interval = self._opts.get("interval", 15)
            set_interval_resp = await self._async_client.get(
                _SET_REQUEST_ENDPOINT.format(
                    serial_number=self._serial_number,
                    interval=interval,
                ),
                timeout=30,
                cookies=self._session,
            )
            self._check_response(set_interval_resp)

            # Fetch monitor data
            monitor_resp = await self._async_client.get(
                _MONITOR_ENDPOINT.format(serial_number=self._serial_number),
                timeout=30,
                cookies=self._session,
            )
            self._check_response(monitor_resp)

            try:
                new_data = monitor_resp.json()
                if not new_data:
                    _LOGGER.warning("Empty JSON response from monitor")
                    raise AtonStorageConnectionError("Empty response")
                self.data.update(new_data)
                _LOGGER.debug("Monitor data fetched successfully")
            except (ValueError, json.JSONDecodeError) as exc:
                _LOGGER.error("Invalid JSON in monitor response. Session might be invalid.")
                _LOGGER.debug("Response text: %s", monitor_resp.text)
                self._session = None  # Force re-login
                raise AtonStorageConnectionError("Invalid JSON in monitor response") from exc

            # Fetch energy data (hack fix)
            if self._id_plant:
                now = datetime.now()
                energy_resp = await self._async_client.get(
                    _ENERGY_ENDPOINT.format(
                        id=self._id_plant,
                        year=now.year,
                        month=now.month,
                        day=now.day,
                    ),
                    timeout=30,
                    cookies=self._session,
                )
                self._check_response(energy_resp)

                try:
                    energy_data = energy_resp.json()
                    if energy_data and "tot_pReteOut" in energy_data:
                        self.data["eVenduta"] = energy_data["tot_pReteOut"]
                        _LOGGER.debug("Energy data fetched successfully")
                except (ValueError, json.JSONDecodeError):
                    _LOGGER.warning("Invalid JSON in energy response")
                    # We don't necessarily clear session here if monitor worked, 
                    # but it's a bad sign.

        except httpx.HTTPError as exc:
            _LOGGER.error("HTTP error during refresh: %s", exc)
            self._session = None # Might be a network issue or session issue, clearing to be safe
            raise AtonStorageConnectionError(f"HTTP error: {exc}") from exc
        except AtonStorageConnectionError:
            raise
        except Exception as exc:
            _LOGGER.error("Unexpected error during refresh: %s", exc)
            raise exc

    def get_raw_data(self, __name: str):
        return self.data.get(__name)

    @property
    def grid_to_house(self) -> bool:
        return int(self.data["status"]) & 1 == 1

    @property
    def solar_to_battery(self) -> bool:
        return int(self.data["status"]) & 2 == 2

    @property
    def solar_to_grid(self) -> bool:
        return int(self.data["status"]) & 4 == 4

    @property
    def battery_to_house(self) -> bool:
        return int(self.data["status"]) & 8 == 8

    @property
    def solar_to_house(self) -> bool:
        return int(self.data["status"]) & 16 == 16

    @property
    def grid_to_battery(self) -> bool:
        return int(self.data["status"]) & 32 == 32

    @property
    def battery_to_grid(self) -> bool:
        return int(self.data["status"]) & 64 == 64

    @property
    def serial_number(self) -> str:
        return self.data["serialNumber"]

    @property
    def last_update(self) -> str:
        return self.data["data"]

    @property
    def status(self) -> str:
        return self.data["status"]

    @property
    def status_man(self) -> str:
        return self.data["statusMan"]

    @property
    def instant_solar_power(self) -> int:
        return int(self.data["pSolare"])

    @property
    def instant_user_power(self) -> int:
        return int(self.data["pUtenze"])

    @property
    def instant_user_power_real(self) -> int:
        return int(self.data["pUtenzeReal"])

    @property
    def instant_battery_power(self) -> int:
        return int(self.data["pBatteria"])

    @property
    def instant_grid_input_power(self) -> int:
        return int(self.data["pReteIn"])

    @property
    def instant_grid_output_power(self) -> int:
        return int(self.data["pReteOut"])

    @property
    def instant_grid_power(self) -> int:
        return int(self.data["pRete"])

    @property
    def instant_grid_power_real(self) -> int:
        return int(self.data["pReteReal"])

    @property
    def status_of_charge(self) -> float:
        return float(self.data["soc"])

    @property
    def run_mode(self) -> int:
        return int(self.data["runMode"])

    @property
    def string1_current(self) -> float:
        return float(self.data["string1I"])

    @property
    def string1_voltage(self) -> float:
        return float(self.data["string1V"])

    @property
    def string2_current(self) -> float:
        return float(self.data["string2I"])

    @property
    def string2_voltage(self) -> float:
        return float(self.data["string2V"])

    @property
    def user_current(self) -> float:
        return float(self.data["utenzeI"])

    @property
    def user_voltage(self) -> float:
        return float(self.data["utenzeV"])

    @property
    def battery_voltage(self) -> float:
        return float(self.data["vb"])

    @property
    def battery_current(self) -> float:
        return float(self.data["ib"])

    @property
    def fw_Scheda(self) -> str:
        return self.data["fwScheda"]

    @property
    def rel_inverter(self) -> str:
        return self.data["relInverter"]

    @property
    def rel_manager(self) -> str:
        return self.data["relManager"]

    @property
    def rel_charger(self) -> str:
        return self.data["relCharger"]

    @property
    def rel_bios(self) -> str:
        return self.data["relBIOS"]

    @property
    def charged(self) -> int:
        return int(self.data["ahCaricati"])

    @property
    def discharge(self) -> int:
        return int(self.data["ahScaricati"])

    @property
    def max_selled_power(self) -> int:
        return self.data["pMaxVenduta"]

    @property
    def max_pannel_power(self) -> int:
        return self.data["pMaxPannelli"]

    @property
    def max_battery_power(self) -> int:
        return self.data["pMaxBatteria"]

    @property
    def max_bought_power(self) -> int:
        return self.data["pMaxComprata"]

    @property
    def selled_energy(self) -> int:
        return self.data["eVenduta"]

    @property
    def pannel_energy(self) -> int:
        return self.data["ePannelli"]

    @property
    def self_consumed_energy(self) -> int:
        return self.data["eBatteria"]

    @property
    def bought_energy(self) -> int:
        return self.data["eComprata"]

    @property
    def consumed_energy(self) -> int:
        return int(self.bought_energy) + int(self.self_consumed_energy)

    # "ingressi1": "0",
    # "ingressi2": "160",
    # "ingressi3": "0",
    # "ingressi4": "0",
    # "ingressi5": "0",
    # "ingressi6": "0",
    # "ingressi7": "0",
    # "ingressi8": "0",
    # "uscite1": "0",
    # "uscite2": "10",
    # "uscite3": "0",
    # "uscite4": "0",
    # "uscite5": "0",
    # "uscite6": "0",
    # "iac1": "0",
    # "iac2": "0",
    # "iac3": "0",
    # "allarmi1": "0",
    # "allarmi2": "0",
    # "allarmi3": "0",
    # "allarmi4": "0",
    # "allarmi5": "0",
    # "allarmi6": "0",
    # "allarmi7": "0",
    # "allarmi8": "0",
    # "allarmi9": "0",
    # "allarmi10": "0",
    # "allarmi11": "0",
    # "allarmi12": "32",
    # "allarmi13": "0",
    # "allarmi14": "0",
    # "allarmi15": "0",
    # "allarmi16": "0",

    @property
    def grid_voltage(self) -> float:
        return self.data["gridV"]

    @property
    def grid_frequency(self) -> float:
        return self.data["gridHz"]

    @property
    def grid_power(self) -> float:
        return self.data["pGrid"]

    # "string1IIN": "0",
    # "string1VIN": "0",
    # "string2IIN": "0",
    # "string2VIN": "0",

    @property
    def temperature(self) -> float:
        return self.data["temperatura"]

    @property
    def temperature2(self) -> float:
        return self.data["temperatura2"]

    # "dataAllarme": "07/11/2022 07:11:28",

    @property
    def update_delay(self) -> int:
        return self.data["DiffDate"]

    # "DiffDate": "829",
    # "timestampScheda": "07/11/2022 11:13:13",

    @property
    def vb_scheda(self) -> str:
        return self.data["vbScheda"] | None

    # "flagProgrammazione": "128",
    # "flagProgrammazione3": "72",
    # "wifi": "1",
    # "exportLimit": "0",

    # "pL1": "0",
    # "pL2": "0",
    # "pL3": "0",
    # "pReteL1": "0",
    # "pReteL2": "0",
    # "pReteL3": "0",

    @property
    def ev_num(self) -> int:
        return int(self.data["num_EV"])

    @property
    def ev_status_of_charge(self) -> float:
        return float(self.data["SoC_EV"])

    @property
    def ev_status(self) -> int:
        return int(self.data["stato_EV"])

    # var firstNumber = (parseInt(_data.stato_EV)&0xf0)>>4;
    # var secondNumber = parseInt(_data.stato_EV)&0x0f;

    @property
    def ev_status_off(self) -> bool:
        return int(self.data["stato_EV"]) & 0xF0 >> 4 == 0 or (
            int(self.data["stato_EV"]) & 0xF0 >> 4 == 1
            and int(self.data["stato_EV"]) & 0x0F != 3
        )

    @property
    def ev_status_on(self) -> bool:
        return (
            int(self.data["stato_EV"]) & 0xF0 >> 4 == 1
            and int(self.data["stato_EV"]) & 0x0F == 3
        )

    @property
    def ev_status_charge(self) -> bool:
        return int(self.data["stato_EV"]) & 0xF0 >> 4 == 2

    @property
    def ev_status_warning(self) -> bool:
        return (
            int(self.data["stato_EV"]) & 0xF0 >> 4 == 4
            or int(self.data["stato_EV"]) & 0xF0 >> 4 == 5
        )

    @property
    def ev_setp(self) -> float:
        return float(self.data["setp_EV"])  # in A

    @property
    def ev_power(self) -> int:
        return int(self.data["potenza_EV"])  # carica in W

    @property
    def ev_kmh(self) -> float:
        return float(self.data["kmh"])  # evCaricakmh km/h

    @property
    def ev_e_ciclo_(self) -> float:
        return float(self.data["e_ciclo_EV"])  # evScaricakWh

    @property
    def ev_km(self) -> float:
        return float(self.data["km"])  # evScaricakm km

    @property
    def ev_perc_carica(self) -> float:
        return float(self.data["perc_carica"])  # evCaricakmh %

    # "paese": "IT",
    # "scena": "0",
    # "qeps": "1",
    # "allertaMeteoAuto": "0",

    @property
    def battery_count(self) -> int:
        return self.data["numBatterie"]


class AtonStorageConnectionError(Exception):
    """Unable to start fetching data."""


class UsernameAndPasswordRequiredError(Exception):
    """Error username and password required."""


class InvalidUsernameOrPasswordError(Exception):
    """Error invalid username or password."""


class SerialNumberRequiredError(Exception):
    """Error to serial number required."""

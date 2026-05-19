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
            login_page = await self._async_client.get(
                _LOGIN_ENDPOINT, timeout=30, follow_redirects=True
            )
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
                follow_redirects=True,
            )
            login_resp.raise_for_status()

            if login_resp.cookies.get("PHPSESSID") or login_resp.headers.get("Set-Cookie"):
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
                _LOGGER.error("Login failed: No session cookie received")
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
                raise AtonStorageConnectionError("Could not login to Aton server")

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
                follow_redirects=True,
            )
            self._check_response(set_interval_resp)

            # Fetch monitor data
            monitor_resp = await self._async_client.get(
                _MONITOR_ENDPOINT.format(serial_number=self._serial_number),
                timeout=30,
                cookies=self._session,
                follow_redirects=True,
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
                _LOGGER.warning("REST result could not be parsed as JSON")
                _LOGGER.debug(
                    "Invalid JSON in monitor response. Status: %s, Body snippet: %s",
                    monitor_resp.status_code,
                    monitor_resp.text[:200],
                )
                if "Unauthorized" in monitor_resp.text or monitor_resp.status_code == 401:
                    self._session = None  # Force re-login only if definitely unauthorized
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
                    follow_redirects=True,
                )
                # We don't necessarily want to fail the whole refresh if energy data fails
                try:
                    self._check_response(energy_resp)
                    energy_data = energy_resp.json()
                    if energy_data and "tot_pReteOut" in energy_data:
                        self.data["eVenduta"] = energy_data["tot_pReteOut"]
                        _LOGGER.debug("Energy data fetched successfully")
                except (ValueError, json.JSONDecodeError, httpx.HTTPError) as exc:
                    _LOGGER.warning("Could not fetch energy data: %s", exc)

        except httpx.HTTPError as exc:
            _LOGGER.error("HTTP error during refresh: %s", exc)
            # Only clear session if it looks like a persistent session issue
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
                self._session = None
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
        return int(self.data.get("status", 0)) & 1 == 1

    @property
    def solar_to_battery(self) -> bool:
        return int(self.data.get("status", 0)) & 2 == 2

    @property
    def solar_to_grid(self) -> bool:
        return int(self.data.get("status", 0)) & 4 == 4

    @property
    def battery_to_house(self) -> bool:
        return int(self.data.get("status", 0)) & 8 == 8

    @property
    def solar_to_house(self) -> bool:
        return int(self.data.get("status", 0)) & 16 == 16

    @property
    def grid_to_battery(self) -> bool:
        return int(self.data.get("status", 0)) & 32 == 32

    @property
    def battery_to_grid(self) -> bool:
        return int(self.data.get("status", 0)) & 64 == 64

    @property
    def serial_number(self) -> str:
        return self.data.get("serialNumber")

    @property
    def last_update(self) -> str:
        return self.data.get("data")

    @property
    def status(self) -> str:
        return self.data.get("status")

    @property
    def status_man(self) -> str:
        return self.data.get("statusMan")

    @property
    def instant_solar_power(self) -> int:
        return int(self.data.get("pSolare", 0))

    @property
    def instant_user_power(self) -> int:
        return int(self.data.get("pUtenze", 0))

    @property
    def instant_user_power_real(self) -> int:
        return int(self.data.get("pUtenzeReal", 0))

    @property
    def instant_battery_power(self) -> int:
        return int(self.data.get("pBatteria", 0))

    @property
    def instant_grid_input_power(self) -> int:
        return int(self.data.get("pReteIn", 0))

    @property
    def instant_grid_output_power(self) -> int:
        return int(self.data.get("pReteOut", 0))

    @property
    def instant_grid_power(self) -> int:
        return int(self.data.get("pRete", 0))

    @property
    def instant_grid_power_real(self) -> int:
        return int(self.data.get("pReteReal", 0))

    @property
    def status_of_charge(self) -> float:
        return float(self.data.get("soc", 0))

    @property
    def run_mode(self) -> int:
        return int(self.data.get("runMode", 0))

    @property
    def string1_current(self) -> float:
        return float(self.data.get("string1I", 0))

    @property
    def string1_voltage(self) -> float:
        return float(self.data.get("string1V", 0))

    @property
    def string2_current(self) -> float:
        return float(self.data.get("string2I", 0))

    @property
    def string2_voltage(self) -> float:
        return float(self.data.get("string2V", 0))

    @property
    def user_current(self) -> float:
        return float(self.data.get("utenzeI", 0))

    @property
    def user_voltage(self) -> float:
        return float(self.data.get("utenzeV", 0))

    @property
    def battery_voltage(self) -> float:
        return float(self.data.get("vb", 0))

    @property
    def battery_current(self) -> float:
        return float(self.data.get("ib", 0))

    @property
    def fw_Scheda(self) -> str:
        return self.data.get("fwScheda")

    @property
    def rel_inverter(self) -> str:
        return self.data.get("relInverter")

    @property
    def rel_manager(self) -> str:
        return self.data.get("relManager")

    @property
    def rel_charger(self) -> str:
        return self.data.get("relCharger")

    @property
    def rel_bios(self) -> str:
        return self.data.get("relBIOS")

    @property
    def charged(self) -> int:
        return int(self.data.get("ahCaricati", 0))

    @property
    def discharge(self) -> int:
        return int(self.data.get("ahScaricati", 0))

    @property
    def max_selled_power(self) -> int:
        return self.data.get("pMaxVenduta", 0)

    @property
    def max_pannel_power(self) -> int:
        return self.data.get("pMaxPannelli", 0)

    @property
    def max_battery_power(self) -> int:
        return self.data.get("pMaxBatteria", 0)

    @property
    def max_bought_power(self) -> int:
        return self.data.get("pMaxComprata", 0)

    @property
    def selled_energy(self) -> int:
        return self.data.get("eVenduta", 0)

    @property
    def pannel_energy(self) -> int:
        return self.data.get("ePannelli", 0)

    @property
    def self_consumed_energy(self) -> int:
        return self.data.get("eBatteria", 0)

    @property
    def bought_energy(self) -> int:
        return self.data.get("eComprata", 0)

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
        return float(self.data.get("gridV", 0))

    @property
    def grid_frequency(self) -> float:
        return float(self.data.get("gridHz", 0))

    @property
    def grid_power(self) -> float:
        return float(self.data.get("pGrid", 0))

    # "string1IIN": "0",
    # "string1VIN": "0",
    # "string2IIN": "0",
    # "string2VIN": "0",

    @property
    def temperature(self) -> float:
        return float(self.data.get("temperatura", 0))

    @property
    def temperature2(self) -> float:
        return float(self.data.get("temperatura2", 0))

    # "dataAllarme": "07/11/2022 07:11:28",

    @property
    def update_delay(self) -> int:
        return int(self.data.get("DiffDate", 0))

    # "DiffDate": "829",
    # "timestampScheda": "07/11/2022 11:13:13",

    @property
    def vb_scheda(self) -> str:
        return self.data.get("vbScheda")

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
        stato_ev = int(self.data.get("stato_EV", 0))
        return (stato_ev & 0xF0) >> 4 == 0 or (
            (stato_ev & 0xF0) >> 4 == 1 and (stato_ev & 0x0F) != 3
        )

    @property
    def ev_status_on(self) -> bool:
        stato_ev = int(self.data.get("stato_EV", 0))
        return (stato_ev & 0xF0) >> 4 == 1 and (stato_ev & 0x0F) == 3

    @property
    def ev_status_charge(self) -> bool:
        stato_ev = int(self.data.get("stato_EV", 0))
        return (stato_ev & 0xF0) >> 4 == 2

    @property
    def ev_status_warning(self) -> bool:
        stato_ev = int(self.data.get("stato_EV", 0))
        return (stato_ev & 0xF0) >> 4 == 4 or (stato_ev & 0xF0) >> 4 == 5

    @property
    def ev_setp(self) -> float:
        return float(self.data.get("setp_EV", 0))  # in A

    @property
    def ev_power(self) -> int:
        return int(self.data.get("potenza_EV", 0))  # carica in W

    @property
    def ev_kmh(self) -> float:
        return float(self.data.get("kmh", 0))  # evCaricakmh km/h

    @property
    def ev_e_ciclo_(self) -> float:
        return float(self.data.get("e_ciclo_EV", 0))  # evScaricakWh

    @property
    def ev_km(self) -> float:
        return float(self.data.get("km", 0))  # evScaricakm km

    @property
    def ev_perc_carica(self) -> float:
        return float(self.data.get("perc_carica", 0))  # evCaricakmh %

    # "paese": "IT",
    # "scena": "0",
    # "qeps": "1",
    # "allertaMeteoAuto": "0",

    @property
    def battery_count(self) -> int:
        return int(self.data.get("numBatterie", 0))


class AtonStorageConnectionError(Exception):
    """Unable to start fetching data."""


class UsernameAndPasswordRequiredError(Exception):
    """Error username and password required."""


class InvalidUsernameOrPasswordError(Exception):
    """Error invalid username or password."""


class SerialNumberRequiredError(Exception):
    """Error to serial number required."""

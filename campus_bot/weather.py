from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

from .network import ServiceError, get_json

DESCRIPTIONS = {0: "ясно ☀️", 1: "преимущественно ясно 🌤", 2: "переменная облачность ⛅",
                3: "пасмурно ☁️", 45: "туман 🌫", 48: "изморозь",
                51: "слабая морось", 53: "морось", 55: "сильная морось",
                56: "ледяная морось", 57: "ледяная морось",
                61: "небольшой дождь 🌧", 63: "дождь 🌧", 65: "сильный дождь 🌧",
                66: "ледяной дождь", 67: "ледяной дождь", 71: "слабый снег 🌨",
                73: "снег 🌨", 75: "сильный снег 🌨", 77: "снежная крупа",
                80: "небольшой ливень 🌦", 81: "ливень 🌧", 82: "сильный ливень 🌧",
                85: "снежный ливень", 86: "сильный снежный ливень", 95: "гроза ⛈",
                96: "гроза с градом ⛈", 99: "гроза с градом ⛈"}


class Weather:
    def __init__(self, store, tz, fetch=get_json):
        self.store, self.tz, self.fetch = store, tz, fetch

    async def today(self, now=None):
        now = now or datetime.now(self.tz)
        key = "weather:" + now.date().isoformat()
        cached = self.store.cache_get(key)
        stale = False
        expires = datetime.fromisoformat(cached[1]) + timedelta(minutes=30) if cached else now
        if cached and cached[0].get("_http_expires"):
            try:
                expires = max(expires, parsedate_to_datetime(cached[0]["_http_expires"]))
            except (ValueError, TypeError):
                pass
        if cached and now < expires:
            data = cached[0]
        else:
            query = urlencode({
                "latitude": 45.0355, "longitude": 38.9753, "timezone": "Europe/Moscow",
                "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
                "forecast_days": 1, "wind_speed_unit": "ms",
            })
            providers = [
                "https://api.met.no/weatherapi/locationforecast/2.0/compact?lat=45.0355&lon=38.9753",
                "https://api.open-meteo.com/v1/forecast?" + query,
            ]
            data = None
            for url in providers:
                try:
                    candidate = await self.fetch(url, "Погода")
                    self._format(candidate, now.date().isoformat(), now)
                    data = candidate
                    self.store.cache_put(key, data, now.isoformat())
                    break
                except (ServiceError, ValueError, KeyError, IndexError, TypeError):
                    continue
            if data is None:
                if not cached:
                    return "🌦 Краснодар: погода временно недоступна."
                data, stale = cached[0], True
        try:
            result = self._format(data, now.date().isoformat(), now)
        except (ValueError, KeyError, IndexError, TypeError):
            return "🌦 Краснодар: погода временно недоступна."
        if stale:
            result += "\n⚠️ Кэш погоды от " + datetime.fromisoformat(cached[1]).strftime("%H:%M")
        return result

    @staticmethod
    def _format(data, day, now=None):
        if "properties" in data:
            return Weather._format_met(data, day, now)
        current, daily = data["current"], data["daily"]
        if daily["time"][0] != day:
            raise ValueError("Погода на другую дату")
        description = DESCRIPTIONS.get(current["weather_code"], "переменная погода")
        rain = daily["precipitation_probability_max"][0]
        result = (f"🌦 Краснодар · {description}\n"
                  f"Сейчас {current['temperature_2m']:+.0f}°C, ощущается {current['apparent_temperature']:+.0f}°C\n"
                  f"За день {daily['temperature_2m_min'][0]:+.0f}…{daily['temperature_2m_max'][0]:+.0f}°C · "
                  f"осадки {rain}% · ветер {current['wind_speed_10m']:.0f} м/с")
        if rain >= 50:
            result += "\n☂️ Возьмите зонт."
        if current["wind_speed_10m"] >= 10:
            result += "\n💨 Сегодня ветрено."
        return result + "\nДанные: Open-Meteo · https://open-meteo.com/ · CC BY 4.0"

    @staticmethod
    def _format_met(data, day, now):
        points = [(datetime.fromisoformat(p["time"].replace("Z", "+00:00")).astimezone(now.tzinfo), p["data"])
                  for p in data["properties"]["timeseries"]]
        remaining = [(when, point) for when, point in points if when.date().isoformat() == day]
        if not remaining:
            raise ValueError("Нет прогноза на сегодня")
        when, point = min(remaining, key=lambda p: abs((p[0] - now).total_seconds()))
        if abs((when - now).total_seconds()) > 3 * 3600:
            raise ValueError("Прогноз слишком далеко от текущего времени")
        instant = point["instant"]["details"]
        symbol = point.get("next_1_hours", point.get("next_6_hours", {})).get("summary", {}).get("symbol_code", "")
        symbol = symbol.split("_")[0]
        if "thunder" in symbol:
            description = "гроза ⛈"
        elif "snow" in symbol or "sleet" in symbol:
            description = "снег / мокрый снег 🌨"
        elif "rain" in symbol:
            description = "дождь 🌧"
        else:
            description = {"clearsky": "ясно ☀️", "fair": "малооблачно 🌤", "partlycloudy": "переменная облачность ⛅",
                           "cloudy": "пасмурно ☁️", "fog": "туман 🌫"}.get(symbol, "переменная погода")
        temperatures = [p["instant"]["details"]["air_temperature"] for _, p in remaining]
        rain = sum(p.get("next_1_hours", {}).get("details", {}).get("precipitation_amount", 0) for _, p in remaining)
        result = (f"🌦 Краснодар · {description}\n"
                  f"Ближайший прогноз ({when:%H:%M}): {instant['air_temperature']:+.0f}°C · "
                  f"ветер {instant['wind_speed']:.0f} м/с\n"
                  f"Доступные часы сегодня: {min(temperatures):+.0f}…{max(temperatures):+.0f}°C · осадки {rain:.1f} мм")
        if rain >= 0.5:
            result += "\n☂️ Возьмите зонт."
        if instant["wind_speed"] >= 10:
            result += "\n💨 Сегодня ветрено."
        return (result + "\nИсточник: MET Norway · https://www.met.no/ · CC BY 4.0\n"
                "Обработка: перевод и сводка. Лицензия: https://creativecommons.org/licenses/by/4.0/")

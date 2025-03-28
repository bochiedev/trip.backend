from datetime import timedelta
from django.conf import settings
import base64
import io
import math
import base64
from PIL import Image, ImageDraw
from api.models import Trip
import asyncio
import aiohttp
import zlib
from hashlib import md5
from django.core.cache import cache
import json
import logging
from scipy.spatial import KDTree
import numpy as np


logger = logging.getLogger(__name__)



OVERPASS_URL = "http://overpass-api.de/api/interpreter"


async def fetch_overpass_data(session, query, cache_timeout=86400):
    """
    Asynchronously fetches data from the Overpass API with caching.

    Args:
        session (aiohttp.ClientSession): The session to use for the request.
        query (str): The Overpass query.
        cache_timeout (int): Cache duration in seconds (default: 24 hours).

    Returns:
        dict: Parsed JSON response or empty result if request fails.
    """

    # Generate a unique cache key based on the query
    cache_key = md5(query.encode()).hexdigest()

    # Check if data exists in cache
    cached_result = await cache.aget(cache_key)
    if cached_result:
        logger.info(f"Cache hit! {query} Returning cached data.")
        decompressed_data = zlib.decompress(cached_result).decode()
        return json.loads(decompressed_data)

    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        async with session.post(OVERPASS_URL, data=query, headers=headers) as response:
            response.raise_for_status()
            data = await response.text()
            json_data = json.loads(data)

            # Cache compressed response as a string
            await cache.aset(cache_key, zlib.compress(data.encode()), timeout=cache_timeout)
            logger.info(f"No cache! {query} Returning API Data.")
            return json_data

    except aiohttp.ClientError as e:
        logger.error(f"Overpass API request failed: {e}")
        return {"elements": []}


async def get_fuel_stations_data(bbox, session):
    overpass_query_fuel = f"""
    [out:json][timeout:30];
    node["amenity"="fuel"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
    out skel qt;
    """
    data = await fetch_overpass_data(session, overpass_query_fuel)
    return [
        {
            "lat": element['lat'],
            "lon": element['lon'],
            "location": element.get('tags', {}).get('name', 'Fuel Station'),
            "distance": 0.0
        }
        for element in data.get('elements', [])
    ]

async def get_rest_stops_data(bbox, session):
    overpass_query_rest = f"""
    [out:json][timeout:30];
    node["highway"="rest_area"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
    out skel qt;
    """
    data = await fetch_overpass_data(session, overpass_query_rest)
    return [
        {
            "lat": element['lat'],
            "lon": element['lon'],
            "location": element.get('tags', {}).get('name', 'Rest Area'),
            "distance": 0.0
        }
        for element in data.get('elements', [])
    ]

async def get_overpass_data(geometry):
    lats = [coord[1] for coord in geometry]
    lons = [coord[0] for coord in geometry]
    bbox = (min(lats), min(lons), max(lats), max(lons)) 

    async with aiohttp.ClientSession() as session:
        fuel_task = get_fuel_stations_data(bbox, session)
        rest_task = get_rest_stops_data(bbox, session)

        fuel_stations, rest_stops = await asyncio.gather(fuel_task, rest_task)

    return {"fuel_stations": fuel_stations, "rest_stops": rest_stops}

def get_overpass_data_sync(geometry):
    overpass_data = asyncio.run(get_overpass_data(geometry))
    return overpass_data



def preprocess_geometry(geometry):
    """Precomputes segment midpoints and cumulative distances."""
    midpoints = []
    segment_distances = [0]

    total_distance = 0
    for i in range(len(geometry) - 1):
        lon1, lat1 = geometry[i]
        lon2, lat2 = geometry[i + 1]

        # Compute segment midpoint
        mid_lat = (lat1 + lat2) / 2
        mid_lon = (lon1 + lon2) / 2
        midpoints.append((mid_lon, mid_lat))

        segment_distance = haversine(lon1, lat1, lon2, lat2)
        total_distance += segment_distance
        segment_distances.append(total_distance)

    return np.array(midpoints), segment_distances


def calculate_trip(trip_id, distance, duration, current_cycle_hours, geometry, pickup_coords, start_coords, end_coords):
    distance_miles = distance * 0.621371
    
    speed_mph = 60
    driving_hours = distance_miles / speed_mph
    total_on_duty_hours = driving_hours

    stops = []
    current_miles = 0
    current_driving_hours = 0
    current_window_hours = 0
    current_cycle = current_cycle_hours
    day = 1
    time_in_day = 0

    # Calculate cumulative distances along the geometry
    geometry_distances = [0]
    for i in range(1, len(geometry)):
        lon1, lat1 = geometry[i-1]
        lon2, lat2 = geometry[i]
        distance_km = haversine(lon1, lat1, lon2, lat2)
        distance_miles_segment = distance_km * 0.621371
        geometry_distances.append(geometry_distances[-1] + distance_miles_segment)

    total_route_miles = geometry_distances[-1]

    overpass_data = get_overpass_data_sync(geometry)

    fuel_stations = overpass_data['fuel_stations']
    rest_stops = overpass_data['rest_stops']


    pois = fuel_stations + rest_stops

    midpoints, segment_distances = preprocess_geometry(geometry)
    tree = KDTree(midpoints)

    for poi in pois:
        poi_lat, poi_lon = poi['lat'], poi['lon']
        
        _, nearest_idx = tree.query([poi_lon, poi_lat])  
        poi['distance'] = segment_distances[nearest_idx]

    fuel_stations.sort(key=lambda x: x['distance'])
    rest_stops.sort(key=lambda x: x['distance'])

    # Step 2: Check if start and pickup points are the same
    is_start_pickup_same = (
        abs(start_coords.latitude - pickup_coords.latitude) < 0.0001 and
        abs(start_coords.longitude - pickup_coords.longitude) < 0.0001
    )

    # Step 3: Add the start point (and pickup point if they are the same)
    if is_start_pickup_same:
        # Combine Start and Pickup into one stop
        stops.append({
            "location": "Start/Pickup",
            "time": time_in_day,
            "duty_status": "on_duty_not_driving",
            "duration": 1,  # Keep the 1-hour duration for pickup activities
            "lat": start_coords.latitude,
            "lon": start_coords.longitude,
            "miles_traveled": current_miles
        })
        total_on_duty_hours += 1
        time_in_day += 1
        current_window_hours += 1
    else:
        # Add Start point
        stops.append({
            "location": "Start",
            "time": time_in_day,
            "duty_status": "on_duty_not_driving",
            "duration": 0,
            "lat": start_coords.latitude,
            "lon": start_coords.longitude,
            "miles_traveled": current_miles
        })

        # Calculate distance to pickup and add pickup stop
        pickup_index = None
        for i, coord in enumerate(geometry):
            if abs(coord[0] - pickup_coords.longitude) < 0.0001 and abs(coord[1] - pickup_coords.latitude) < 0.0001:
                pickup_index = i
                break
        if pickup_index is None:
            pickup_index = 1
        start_to_pickup_miles = geometry_distances[pickup_index]
        current_miles = start_to_pickup_miles
        hours_to_pickup = start_to_pickup_miles / speed_mph
        total_on_duty_hours += hours_to_pickup
        time_in_day += hours_to_pickup
        current_window_hours += hours_to_pickup
        current_driving_hours += hours_to_pickup

        stops.append({
            "location": "Pickup",
            "time": time_in_day,
            "duty_status": "on_duty_not_driving",
            "duration": 1,
            "lat": pickup_coords.latitude,
            "lon": pickup_coords.longitude,
            "miles_traveled": current_miles
        })
        total_on_duty_hours += 1
        time_in_day += 1
        current_window_hours += 1

    # Step 4: Add real fueling and rest stops during the trip
    fuel_index = 0
    rest_index = 0
    last_fuel_miles = -1000
    last_rest_miles = -8 * speed_mph

    while current_miles < distance_miles:
        if current_cycle + total_on_duty_hours >= 70:
            stop_coords = get_coords_at_distance(current_miles, geometry, geometry_distances, total_route_miles)
            stops.append({
                "location": "Restart",
                "time": time_in_day,
                "duty_status": "off_duty",
                "duration": 34,
                "lat": stop_coords["lat"],
                "lon": stop_coords["lon"],
                "miles_traveled": current_miles
            })
            time_in_day += 34
            current_cycle = 0
            current_window_hours = 0
            current_driving_hours = 0
            if time_in_day >= 24:
                day += int(time_in_day // 24)
                time_in_day = time_in_day % 24

        if current_miles - last_fuel_miles >= 1000 and fuel_index < len(fuel_stations):
            fuel_stop = fuel_stations[fuel_index]
            if fuel_stop['distance'] <= current_miles:
                stops.append({
                    "location": fuel_stop['location'],
                    "time": time_in_day,
                    "duty_status": "on_duty_not_driving",
                    "duration": 0.5,
                    "lat": fuel_stop['lat'],
                    "lon": fuel_stop['lon'],
                    "miles_traveled": current_miles
                })
                total_on_duty_hours += 0.5
                current_window_hours += 0.5
                time_in_day += 0.5
                last_fuel_miles = current_miles
                fuel_index += 1

        if current_driving_hours >= 8 and rest_index < len(rest_stops):
            rest_stop = rest_stops[rest_index]
            if rest_stop['distance'] <= current_miles:
                stops.append({
                    "location": rest_stop['location'],
                    "time": time_in_day,
                    "duty_status": "off_duty",
                    "duration": 0.5,
                    "lat": rest_stop['lat'],
                    "lon": rest_stop['lon'],
                    "miles_traveled": current_miles
                })
                time_in_day += 0.5
                current_window_hours += 0.5
                current_driving_hours = 0
                last_rest_miles = current_miles
                rest_index += 1
                if time_in_day >= 24:
                    day += int(time_in_day // 24)
                    time_in_day = time_in_day % 24
            else:
                stop_coords = get_coords_at_distance(current_miles, geometry, geometry_distances, total_route_miles)
                stops.append({
                    "location": "Rest Break",
                    "time": time_in_day,
                    "duty_status": "off_duty",
                    "duration": 0.5,
                    "lat": stop_coords["lat"],
                    "lon": stop_coords["lon"],
                    "miles_traveled": current_miles
                })
                time_in_day += 0.5
                current_window_hours += 0.5
                current_driving_hours = 0
                last_rest_miles = current_miles
                if time_in_day >= 24:
                    day += int(time_in_day // 24)
                    time_in_day = time_in_day % 24

        remaining_driving_hours = min(11 - current_driving_hours, 14 - current_window_hours)
        if remaining_driving_hours <= 0:
            stop_coords = get_coords_at_distance(current_miles, geometry, geometry_distances, total_route_miles)
            stops.append({
                "location": "Sleeper Berth",
                "time": time_in_day,
                "duty_status": "sleeper_berth",
                "duration": 7,
                "lat": stop_coords["lat"],
                "lon": stop_coords["lon"],
                "miles_traveled": current_miles
            })
            time_in_day += 7
            stop_coords = get_coords_at_distance(current_miles, geometry, geometry_distances, total_route_miles)
            stops.append({
                "location": "Off Duty",
                "time": time_in_day,
                "duty_status": "off_duty",
                "duration": 3,
                "lat": stop_coords["lat"],
                "lon": stop_coords["lon"],
                "miles_traveled": current_miles
            })
            time_in_day += 3
            current_driving_hours = 0
            current_window_hours = 0
            last_rest_miles = current_miles
            if time_in_day >= 24:
                day += int(time_in_day // 24)
                time_in_day = time_in_day % 24
            continue

        miles_to_drive = min(remaining_driving_hours * speed_mph, distance_miles - current_miles)
        hours_to_drive = miles_to_drive / speed_mph
        current_miles += miles_to_drive
        stop_coords = get_coords_at_distance(current_miles, geometry, geometry_distances, total_route_miles)
        stops.append({
            "location": "Driving",
            "time": time_in_day,
            "duty_status": "driving",
            "duration": hours_to_drive,
            "lat": stop_coords["lat"],
            "lon": stop_coords["lon"],
            "miles_traveled": current_miles
        })
        current_driving_hours += hours_to_drive
        current_window_hours += hours_to_drive
        total_on_duty_hours += hours_to_drive
        time_in_day += hours_to_drive
        if time_in_day >= 24:
            day += int(time_in_day // 24)
            time_in_day = time_in_day % 24

    stops.append({
        "location": "Dropoff",
        "time": time_in_day,
        "duty_status": "on_duty_not_driving",
        "duration": 1,
        "lat": end_coords.latitude,
        "lon": end_coords.longitude,
        "miles_traveled": current_miles
    })
    total_on_duty_hours += 1

    trip_data = {"stops": stops, "total_days": day, "total_on_duty_hours": total_on_duty_hours}

    route =  {
                "distance_miles": distance_miles,
                "duration_hours": duration,
                "geometry": geometry,
                "stops": trip_data["stops"]
            }

    trip = Trip.objects.get(id=trip_id)
    log_sheets = generate_eld_logs(trip_data,trip.created_at.date(), trip.user)
    trip.route_data = route
    trip.log_sheets = log_sheets
    trip.save()
    return trip_data

def haversine(lon1, lat1, lon2, lat2, miles=True):
    """Calculate the great-circle distance between two points on the Earth."""
    R = 3958.8 if miles else 6371  # Use miles or kilometers
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    
    return R * c

def get_coords_at_distance(target_miles, geometry, geometry_distances, total_route_miles):
    """Interpolate coordinates at a given distance along a route."""
    if target_miles <= 0:
        return {"lat": geometry[0][1], "lon": geometry[0][0]}
    if target_miles >= total_route_miles:
        return {"lat": geometry[-1][1], "lon": geometry[-1][0]}

    for i in range(len(geometry_distances) - 1):
        if geometry_distances[i] <= target_miles <= geometry_distances[i + 1]:
            fraction = (target_miles - geometry_distances[i]) / (geometry_distances[i + 1] - geometry_distances[i])
            lon1, lat1 = geometry[i]
            lon2, lat2 = geometry[i + 1]
            interpolated_lat = lat1 + fraction * (lat2 - lat1)
            interpolated_lon = lon1 + fraction * (lon2 - lon1)
            return {"lat": interpolated_lat, "lon": interpolated_lon}

    return {"lat": geometry[-1][1], "lon": geometry[-1][0]}

def generate_eld_logs(trip_data, start_date,user):
    """Generate ELD logs for a trip with stops and timestamps."""
    stops = trip_data["stops"]
    total_days = trip_data["total_days"]
    
    log_sheets = []

    for day in range(1, total_days + 1):
        template_path = settings.BLANK_LOG_TEMPLATE_PATH
        img = Image.open(template_path)
        draw = ImageDraw.Draw(img)

        # Date and Carrier Information
        log_date = start_date + timedelta(days=day - 1)
        draw.text((50, 50), log_date.strftime("%Y-%m-%d"), fill="black")


        draw.text((50, 70), trip_data.get("driver_id", user.driver_number), fill="blue")
        draw.text((50, 90), trip_data.get("vehicle_id", user.truck_number), fill="blue")
        draw.text((50, 110), trip_data.get("carrier_name", "Company Name"), fill="blue")
        draw.text((50, 130), trip_data.get("carrier_address", "123 Main St, City, ST"), fill="blue")

        # Duty Status Graph
        remarks = []
        for stop in stops:
            start_time = stop["time"]
            if (start_time // 24) + 1 != day:
                continue

            start_time %= 24  # Get hour in the current day
            duration = stop["duration"]
            duty_status = stop["duty_status"]

            # Define Y-axis positions for duty statuses
            duty_positions = {
                "off_duty": 50,
                "sleeper_berth": 100,
                "driving": 150,
                "on_duty": 200
            }
            line_y = duty_positions.get(duty_status, 50)

            # Scaling factors
            x_scale = 20  # Pixels per hour
            graph_x_start = 200
            start_x = graph_x_start + (start_time * x_scale)
            end_x = graph_x_start + ((start_time + duration) * x_scale)

            draw.line((start_x, line_y, min(end_x, img.width - 20), line_y), fill="black", width=2)
            remarks.append(f"{stop['location']} at {start_time:.2f} hrs")

        # Add remarks to the log
        for i, remark in enumerate(remarks):
            draw.text((50, 300 + i * 20), remark, fill="black")

        # Convert image to base64
        buffered = io.BytesIO()
        img.save(buffered, format="PNG")
        img_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
        log_sheets.append(img_base64)

    return log_sheets
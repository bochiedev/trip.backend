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

    # Generate cache key based on the query
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
    (
        node["amenity"="fuel"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
        way["amenity"="fuel"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
        node["highway"="services"]["fuel"="yes"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
        way["highway"="services"]["fuel"="yes"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
    );
    out center qt;
    """

    data = await fetch_overpass_data(session, overpass_query_fuel)

    return [
        {
            "lat": element.get('center', {}).get('lat') if 'center' in element else element['lat'],
            "lon": element.get('center', {}).get('lon') if 'center' in element else element['lon'],
            "location": element.get('tags', {}).get('name', 'Fuel Station'),
            "distance": 0.0
        }
        for element in data.get('elements', [])
    ]

async def get_rest_stops_data(bbox, session):
    overpass_query_rest = f"""
    [out:json][timeout:30];
    (
        node["highway"="rest_area"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
        way["highway"="rest_area"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
        node["amenity"="rest_area"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
        way["amenity"="rest_area"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
        node["highway"="services"]["rest_area"="yes"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
        way["highway"="services"]["rest_area"="yes"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
    );
    out center qt;
    """

    data = await fetch_overpass_data(session, overpass_query_rest)
    return [
        {
            "lat": element.get('center', {}).get('lat') if 'center' in element else element['lat'],
            "lon": element.get('center', {}).get('lon') if 'center' in element else element['lon'],
            "location": element.get('tags', {}).get('name', 'Rest Stop'),
            "distance": 0.0
        }
        for element in data.get('elements', [])
    ]

async def get_trailer_changes_data(bbox, session):
    overpass_query_trailer = f"""
    [out:json][timeout:30];
    (
        node["amenity"="truck_stop"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
        way["amenity"="truck_stop"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
        node["highway"="services"]["truck_stop"="yes"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
        way["highway"="services"]["truck_stop"="yes"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
    );
    out center qt;
    """

    data = await fetch_overpass_data(session, overpass_query_trailer)
    return [
        {
            "lat": element.get('center', {}).get('lat') if 'center' in element else element['lat'],
            "lon": element.get('center', {}).get('lon') if 'center' in element else element['lon'],
            "location": element.get('tags', {}).get('name', 'Truck Stop'),
            "distance": 0.0
        }
        for element in data.get('elements', [])
    ]

async def get_inspection_stops_data(bbox, session):
    overpass_query_inspection = f"""
    [out:json][timeout:30];
    (
        node["highway"="weigh_station"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
        way["highway"="weigh_station"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
    );
    out center qt;
    """

    data = await fetch_overpass_data(session, overpass_query_inspection)
    return [
        {
            "lat": element.get('center', {}).get('lat') if 'center' in element else element['lat'],
            "lon": element.get('center', {}).get('lon') if 'center' in element else element['lon'],
            "location": element.get('tags', {}).get('name', 'Weigh Station'),
            "distance": 0.0
        }
        for element in data.get('elements', [])
    ]

async def get_overpass_data(geometry):
    # Segment the route into smaller sections (e.g., every 500 miles)
    segment_length = 500 * 1.60934 
    segments = []
    current_segment = []
    current_distance = 0

    for i in range(1, len(geometry)):
        lon1, lat1 = geometry[i-1]
        lon2, lat2 = geometry[i]
        distance_km = haversine(lon1, lat1, lon2, lat2)
        current_distance += distance_km
        current_segment.append(geometry[i-1])

        if current_distance >= segment_length or i == len(geometry) - 1:
            current_segment.append(geometry[i])
            segments.append(current_segment)
            current_segment = [geometry[i]]
            current_distance = 0

    # Fetch stops for each segment
    all_fuel_stations = []
    all_rest_stops = []
    all_trailer_changes = []
    all_inspection_stops = []
    async with aiohttp.ClientSession() as session:
        for segment in segments:
            lats = [coord[1] for coord in segment]
            lons = [coord[0] for coord in segment]
            bbox = (min(lats), min(lons), max(lats), max(lons))

            fuel_task = get_fuel_stations_data(bbox, session)
            rest_task = get_rest_stops_data(bbox, session)
            trailer_task = get_trailer_changes_data(bbox, session)
            inspection_task = get_inspection_stops_data(bbox, session)

            fuel_stations, rest_stops, trailer_changes, inspection_stops = await asyncio.gather(
                fuel_task, rest_task, trailer_task, inspection_task
            )

            all_fuel_stations.extend(fuel_stations)
            all_rest_stops.extend(rest_stops)
            all_trailer_changes.extend(trailer_changes)
            all_inspection_stops.extend(inspection_stops)

    return {
        "fuel_stations": all_fuel_stations,
        "rest_stops": all_rest_stops,
        "trailer_changes": all_trailer_changes,
        "inspection_stops": all_inspection_stops
    }

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


def find_nearest_midpoint(midpoints, poi_lon, poi_lat):
    distances = np.sqrt((midpoints[:, 0] - poi_lon)**2 + (midpoints[:, 1] - poi_lat)**2)
    nearest_idx = np.argmin(distances)
    return nearest_idx


def calculate_trip(trip_id, distance, duration, current_cycle_hours, geometry, pickup_coords, start_coords, end_coords,
                   scaling_interval=500, break_timing=6, pre_trip_duration=0.5, post_trip_duration=1.5,
                   fueling_duration=0.5, loading_duration=0.5, unloading_duration=0.5, rest_break_duration=0.5):
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
    split_sleeper_used = False

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
    trailer_changes = overpass_data['trailer_changes']
    inspection_stops = overpass_data['inspection_stops']

    pois = fuel_stations + rest_stops + trailer_changes + inspection_stops

    midpoints, segment_distances = preprocess_geometry(geometry)

    for poi in pois:
        poi_lat, poi_lon = poi['lat'], poi['lon']
        nearest_idx = find_nearest_midpoint(midpoints, poi_lon, poi_lat)
        poi['distance'] = segment_distances[nearest_idx]

    fuel_stations.sort(key=lambda x: x['distance'])
    rest_stops.sort(key=lambda x: x['distance'])
    trailer_changes.sort(key=lambda x: x['distance'])
    inspection_stops.sort(key=lambda x: x['distance'])

    #Check if start and pickup points are the same
    is_start_pickup_same = (
        abs(start_coords.latitude - pickup_coords.latitude) < 0.0001 and
        abs(start_coords.longitude - pickup_coords.longitude) < 0.0001
    )

    #Add the start point with a pre-trip inspection
    if is_start_pickup_same:
        stops.append({
            "location": "Start/Pickup",
            "activity": "Pre-trip & TI",
            "time": time_in_day,
            "duty_status": "on_duty_not_driving",
            "duration": pre_trip_duration,
            "lat": start_coords.latitude,
            "lon": start_coords.longitude,
            "miles_traveled": current_miles
        })
        total_on_duty_hours += pre_trip_duration
        time_in_day += pre_trip_duration
        current_window_hours += pre_trip_duration
    else:
        stops.append({
            "location": "Start",
            "activity": "Pre-trip & TI",
            "time": time_in_day,
            "duty_status": "on_duty_not_driving",
            "duration": pre_trip_duration,
            "lat": start_coords.latitude,
            "lon": start_coords.longitude,
            "miles_traveled": current_miles
        })
        total_on_duty_hours += pre_trip_duration
        time_in_day += pre_trip_duration
        current_window_hours += pre_trip_duration

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
            "activity": "Loading",
            "time": time_in_day,
            "duty_status": "on_duty_not_driving",
            "duration": loading_duration,
            "lat": pickup_coords.latitude,
            "lon": pickup_coords.longitude,
            "miles_traveled": current_miles
        })
        total_on_duty_hours += loading_duration
        time_in_day += loading_duration
        current_window_hours += loading_duration

    #Add real fueling, scaling, rest, trailer change, and inspection stops
    fuel_index = 0
    rest_index = 0
    trailer_index = 0
    inspection_index = 0
    last_fuel_miles = -1000
    last_scaling_miles = -scaling_interval
    last_trailer_miles = -600
    last_inspection_miles = -300
    last_rest_miles = -8 * speed_mph
    last_break_time = time_in_day

    while current_miles < distance_miles:
        # Check for 70-hour rule (restart)
        if current_cycle + total_on_duty_hours >= 70:
            stop_coords = get_coords_at_distance(current_miles, geometry, geometry_distances, total_route_miles)
            stops.append({
                "location": "Restart",
                "activity": "34-hour Restart",
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
            last_break_time = time_in_day
            split_sleeper_used = False
            if time_in_day >= 24:
                day += int(time_in_day // 24)
                time_in_day = time_in_day % 24

        # Check for fueling stop (every 1000 miles)
        if current_miles - last_fuel_miles >= 1000 and fuel_index < len(fuel_stations):
            fuel_stop = fuel_stations[fuel_index]
            if fuel_stop['distance'] <= current_miles:
                stops.append({
                    "location": fuel_stop['location'],
                    "activity": "Fueling",
                    "time": time_in_day,
                    "duty_status": "on_duty_not_driving",
                    "duration": fueling_duration,
                    "lat": fuel_stop['lat'],
                    "lon": fuel_stop['lon'],
                    "miles_traveled": current_miles
                })
                total_on_duty_hours += fueling_duration
                current_window_hours += fueling_duration
                time_in_day += fueling_duration
                last_fuel_miles = current_miles
                fuel_index += 1

        # Check for scaling stop
        if current_miles - last_scaling_miles >= scaling_interval:
            stop_coords = get_coords_at_distance(current_miles, geometry, geometry_distances, total_route_miles)
            stops.append({
                "location": "Scaling Stop",
                "activity": "Scaling",
                "time": time_in_day,
                "duty_status": "on_duty_not_driving",
                "duration": 0.5,
                "lat": stop_coords["lat"],
                "lon": stop_coords["lon"],
                "miles_traveled": current_miles
            })
            total_on_duty_hours += 0.5
            current_window_hours += 0.5
            time_in_day += 0.5
            last_scaling_miles = current_miles

        # Check for trailer change
        if current_miles - last_trailer_miles >= 600 and trailer_index < len(trailer_changes):
            trailer_stop = trailer_changes[trailer_index]
            if trailer_stop['distance'] <= current_miles:
                stops.append({
                    "location": trailer_stop['location'],
                    "activity": "Trailer Change",
                    "time": time_in_day,
                    "duty_status": "on_duty_not_driving",
                    "duration": 0.5,
                    "lat": trailer_stop['lat'],
                    "lon": trailer_stop['lon'],
                    "miles_traveled": current_miles
                })
                total_on_duty_hours += 0.5
                current_window_hours += 0.5
                time_in_day += 0.5
                last_trailer_miles = current_miles
                trailer_index += 1

        # Check for in-road inspection
        if current_miles - last_inspection_miles >= 300 and inspection_index < len(inspection_stops):
            inspection_stop = inspection_stops[inspection_index]
            if inspection_stop['distance'] <= current_miles:
                stops.append({
                    "location": inspection_stop['location'],
                    "activity": "In-Road Inspection",
                    "time": time_in_day,
                    "duty_status": "on_duty_not_driving",
                    "duration": 0.25,
                    "lat": inspection_stop['lat'],
                    "lon": inspection_stop['lon'],
                    "miles_traveled": current_miles
                })
                total_on_duty_hours += 0.25
                current_window_hours += 0.25
                time_in_day += 0.25
                last_inspection_miles = current_miles
                inspection_index += 1

        # Check for mandatory 30-minute break within 8 hours of driving
        driving_time_since_break = (time_in_day - last_break_time) - sum(
            stop["duration"] for stop in stops if stop["time"] >= last_break_time and stop["duty_status"] != "driving"
        )
        if driving_time_since_break >= break_timing and current_driving_hours > 0:
            stop_coords = get_coords_at_distance(current_miles, geometry, geometry_distances, total_route_miles)
            stops.append({
                "location": "Rest Break",
                "activity": "30-min Break",
                "time": time_in_day,
                "duty_status": "off_duty",
                "duration": rest_break_duration,
                "lat": stop_coords["lat"],
                "lon": stop_coords["lon"],
                "miles_traveled": current_miles
            })
            time_in_day += rest_break_duration
            current_window_hours += rest_break_duration
            current_driving_hours = 0
            last_break_time = time_in_day
            last_rest_miles = current_miles
            if time_in_day >= 24:
                day += int(time_in_day // 24)
                time_in_day = time_in_day % 24

        # Check for rest stop
        if current_driving_hours >= 8 and rest_index < len(rest_stops):
            rest_stop = rest_stops[rest_index]
            if rest_stop['distance'] <= current_miles:
                stops.append({
                    "location": rest_stop['location'],
                    "activity": "Rest Break",
                    "time": time_in_day,
                    "duty_status": "off_duty",
                    "duration": rest_break_duration,
                    "lat": rest_stop['lat'],
                    "lon": rest_stop['lon'],
                    "miles_traveled": current_miles
                })
                time_in_day += rest_break_duration
                current_window_hours += rest_break_duration
                current_driving_hours = 0
                last_break_time = time_in_day
                last_rest_miles = current_miles
                rest_index += 1
                if time_in_day >= 24:
                    day += int(time_in_day // 24)
                    time_in_day = time_in_day % 24

        # Check for 11-hour driving or 14-hour on-duty limit
        remaining_driving_hours = min(11 - current_driving_hours, 14 - current_window_hours)
        if remaining_driving_hours <= 0:
            stop_coords = get_coords_at_distance(current_miles, geometry, geometry_distances, total_route_miles)
            stops.append({
                "location": "Post-Trip",
                "activity": "Post-trip & TI",
                "time": time_in_day,
                "duty_status": "off_duty",
                "duration": post_trip_duration,
                "lat": stop_coords["lat"],
                "lon": stop_coords["lon"],
                "miles_traveled": current_miles
            })
            time_in_day += post_trip_duration
            current_window_hours += post_trip_duration

            if not split_sleeper_used and current_miles < distance_miles * 0.75:
                stops.append({
                    "location": "Sleeper Berth",
                    "activity": "Sleeper Berth (Split 1)",
                    "time": time_in_day,
                    "duty_status": "sleeper_berth",
                    "duration": 8.0,
                    "lat": stop_coords["lat"],
                    "lon": stop_coords["lon"],
                    "miles_traveled": current_miles
                })
                time_in_day += 8.0
                stops.append({
                    "location": "Sleeper Berth",
                    "activity": "Sleeper Berth (Split 2)",
                    "time": time_in_day,
                    "duty_status": "sleeper_berth",
                    "duration": 2.0,
                    "lat": stop_coords["lat"],
                    "lon": stop_coords["lon"],
                    "miles_traveled": current_miles
                })
                time_in_day += 2.0
                split_sleeper_used = True
            else:
                stops.append({
                    "location": "Sleeper Berth",
                    "activity": "Sleeper Berth",
                    "time": time_in_day,
                    "duty_status": "sleeper_berth",
                    "duration": 10.0 - post_trip_duration,
                    "lat": stop_coords["lat"],
                    "lon": stop_coords["lon"],
                    "miles_traveled": current_miles
                })
                time_in_day += (10.0 - post_trip_duration)

            current_driving_hours = 0
            current_window_hours = 0
            last_break_time = time_in_day
            last_rest_miles = current_miles
            if time_in_day >= 24:
                day += int(time_in_day // 24)
                time_in_day = time_in_day % 24
            continue

        # Add driving segment
        miles_to_drive = min(remaining_driving_hours * speed_mph, distance_miles - current_miles)
        hours_to_drive = miles_to_drive / speed_mph
        current_miles += miles_to_drive
        stop_coords = get_coords_at_distance(current_miles, geometry, geometry_distances, total_route_miles)
        stops.append({
            "location": "Driving",
            "activity": "Driving",
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

    #Add dropoff with unloading
    stops.append({
        "location": "Dropoff",
        "activity": "Unloading",
        "time": time_in_day,
        "duty_status": "on_duty_not_driving",
        "duration": unloading_duration,
        "lat": end_coords.latitude,
        "lon": end_coords.longitude,
        "miles_traveled": current_miles
    })
    total_on_duty_hours += unloading_duration
    time_in_day += unloading_duration

    #Add final post-trip inspection if not already added
    if stops[-1]["activity"] != "Post-trip & TI":
        stop_coords = get_coords_at_distance(current_miles, geometry, geometry_distances, total_route_miles)
        stops.append({
            "location": "Post-Trip",
            "activity": "Post-trip & TI",
            "time": time_in_day,
            "duty_status": "off_duty",
            "duration": post_trip_duration,
            "lat": stop_coords["lat"],
            "lon": stop_coords["lon"],
            "miles_traveled": current_miles
        })
        time_in_day += post_trip_duration

    #Add additional trip data fields
    trip = Trip.objects.get(id=trip_id)
    trip_data = {
        "stops": stops,
        "total_days": day,
        "total_on_duty_hours": total_on_duty_hours,
        "trailer_number": trip.user.trailer_number,
        "shipper":  "N/A",
        "commodity":  "N/A",
        "load_id": "N/A",
        "home_terminal":  "N/A",
        "co_driver": "N/A",
    }

    route = {
        "distance_miles": distance_miles,
        "duration_hours": duration,
        "geometry": geometry,
        "stops": trip_data["stops"]
    }

    log_sheets = generate_eld_logs(trip_data, trip.created_at.date(), trip.user)
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



def generate_eld_logs(trip_data, start_date, user):
    """
    Generate ELD logs for a trip with stops and timestamps, following the truck driver logbook format.
    
    Args:
        trip_data (dict): Contains stops, total days, total on-duty hours, and additional trip details.
        start_date (datetime): The start date of the trip.
        user (object): User object containing driver details (e.g., driver_number, truck_number).
    
    Returns:
        list: List of base64-encoded log sheet images.
    """
    stops = trip_data["stops"]
    total_days = trip_data["total_days"]
    
    log_sheets = []

    for day in range(1, total_days + 1):
        # Load the blank log template
        template_path = settings.BLANK_LOG_TEMPLATE_PATH
        img = Image.open(template_path)
        draw = ImageDraw.Draw(img)

        #Fill in Header Information
        log_date = start_date + timedelta(days=day - 1)
        draw.text((26, 22), log_date.strftime("%Y-%m-%d"), fill="black")

        # Driver and Carrier Information
        draw.text((103, 59), user.driver_number, fill="black")
        draw.text((154, 59), "N/A", fill="black")
        draw.text((205, 59), "N/A", fill="black")
        draw.text((308, 59), trip_data.get("co_driver", "N/A"), fill="black")
        draw.text((308, 89), trip_data.get("home_terminal", "N/A"), fill="black")

        # Calculate total miles driven for this day
        day_stops = [stop for stop in stops if (stop["time"] // 24) + 1 == day]
        daily_miles = 0
        if day_stops:
            driving_stops = [stop for stop in day_stops if stop["duty_status"] == "driving"]
            if driving_stops:
                start_miles = min(stop["miles_traveled"] for stop in driving_stops)
                end_miles = max(stop["miles_traveled"] for stop in driving_stops)
                daily_miles = end_miles - start_miles

        # Vehicle and Shipment Information
        draw.text((26, 89), str(user.truck_number), fill="black") 
        draw.text((77, 89), str(user.trailer_number), fill="black")
        draw.text((128, 89), str(round(daily_miles)), fill="black")
        draw.text((26, 133), trip_data.get("shipper", "N/A"), fill="black")
        draw.text((103, 133), trip_data.get("commodity", "N/A"), fill="black")
        draw.text((180, 133), trip_data.get("load_id", "N/A"), fill="black")

        #Duty Status Graph 
        remarks = []
        duty_totals = {
            "off_duty": 0,
            "sleeper_berth": 0,
            "driving": 0,
            "on_duty": 0
        }
        previous_time = 0  
        previous_status = "off_duty"  
        previous_y = 192  

        # Define Y-axis positions for duty statuses
        duty_positions = {
            "off_duty": 192,        
            "sleeper_berth": 222,   
            "driving": 252,         
            "on_duty": 281          
        }

        # Map duty_status from calculate_trip to logbook duty statuses
        duty_status_mapping = {
            "on_duty_not_driving": "on_duty",
            "off_duty": "off_duty",
            "sleeper_berth": "sleeper_berth",
            "driving": "driving"
        }

        # Scaling factors for the 24-hour grid
        x_scale = 13 
        graph_x_start = 77 
        graph_width = 312 

        for stop in stops:
            start_time = stop["time"]
            if (start_time // 24) + 1 != day:
                continue

            start_time = start_time % 24
            duration = stop["duration"]
            duty_status = duty_status_mapping[stop["duty_status"]]
            location = stop["location"]
            activity = stop.get("activity", location)

            # Calculate the X positions for the start and end of this duty status
            start_x = graph_x_start + (start_time * x_scale)
            end_x = graph_x_start + ((start_time + duration) * x_scale)
            end_x = min(end_x, graph_x_start + graph_width)

            # Get the Y position for the current duty status
            line_y = duty_positions.get(duty_status, 192)

            # Draw a vertical line to transition between duty statuses
            if start_time > previous_time:
                prev_end_x = graph_x_start + (start_time * x_scale)
                draw.line((graph_x_start + (previous_time * x_scale), previous_y, prev_end_x, previous_y), fill="black", width=2)
                duty_totals[previous_status] += (start_time - previous_time)

            # Draw the line for the current duty status
            draw.line((start_x, previous_y, start_x, line_y), fill="black", width=2)
            draw.line((start_x, line_y, end_x, line_y), fill="black", width=2)

            # Add a bracket if the truck didn't move
            if duty_status in ["on_duty", "off_duty"] and stop["duty_status"] != "driving":
                draw.line((start_x, line_y + 5, start_x, line_y + 10), fill="black", width=1)  # Adjusted bracket size
                draw.line((end_x, line_y + 5, end_x, line_y + 10), fill="black", width=1)
                draw.line((start_x, line_y + 7, end_x, line_y + 7), fill="black", width=1)

            # Update duty totals
            duty_totals[duty_status] += duration

            # Add remark for duty status change
            remark = f"{location}, {activity} at {int(start_time)}:{int((start_time % 1) * 60):02d}"
            remarks.append(remark)

            # Update previous values
            previous_time = start_time + duration
            previous_status = duty_status
            previous_y = line_y

        # Draw the final segment of the day
        if previous_time < 24:
            end_x = graph_x_start + (24 * x_scale)
            draw.line((graph_x_start + (previous_time * x_scale), previous_y, end_x, previous_y), fill="black", width=2)
            duty_totals[previous_status] += (24 - previous_time)

        # Add Remarks
        for i, remark in enumerate(remarks):
            draw.text((26, 333 + i * 15), remark, fill="black")

        #Calculate and Add Totals 
        total_hours = sum(duty_totals.values())
        if abs(total_hours - 24) > 0.01:
            print(f"Warning: Total hours for day {day} is {total_hours}, expected 24 hours.")

        draw.text((410, 192), f"{duty_totals['off_duty']:.2f} hrs", fill="black")
        draw.text((410, 222), f"{duty_totals['sleeper_berth']:.2f} hrs", fill="black")
        draw.text((410, 252), f"{duty_totals['driving']:.2f} hrs", fill="black")
        draw.text((410, 281), f"{duty_totals['on_duty']:.2f} hrs", fill="black")

        total_on_duty = duty_totals["driving"] + duty_totals["on_duty"]
        draw.text((410, 333), f"Total On-Duty: {total_on_duty:.2f} hrs", fill="black")

        #Convert Image to Base64
        buffered = io.BytesIO()
        img.save(buffered, format="PNG")
        img_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
        log_sheets.append(img_base64)

    return log_sheets
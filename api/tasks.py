# myapp/tasks.py
from celery import shared_task
from api.helpers.trip_planner import get_coords_at_distance, haversine,generate_eld_logs
from api.models import Trip
import requests


@shared_task
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

    # Step 1: Query Overpass API for fueling stations and rest stops along the route
    # Calculate the bounding box of the route
    lats = [coord[1] for coord in geometry]
    lons = [coord[0] for coord in geometry]
    bbox = (min(lats), min(lons), max(lats), max(lons))  # south,west,north,east

    overpass_url = "http://overpass-api.de/api/interpreter"
    overpass_query_fuel = f"""
    [out:json];
    node["amenity"="fuel"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
    out body;
    """
    try:
        response = requests.post(overpass_url, data=overpass_query_fuel)
        response.raise_for_status()
        fuel_data = response.json()
        fuel_stations = [
            {
                "lat": element['lat'],
                "lon": element['lon'],
                "location": element.get('tags', {}).get('name', 'Fuel Station'),
                "distance": 0.0
            }
            for element in fuel_data['elements']
        ]
    except requests.RequestException as e:
        print(f"Overpass fuel query failed: {e}")
        fuel_stations = []

    overpass_query_rest = f"""
    [out:json];
    node["highway"="rest_area"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
    out body;
    """
    try:
        response = requests.post(overpass_url, data=overpass_query_rest)
        response.raise_for_status()
        rest_data = response.json()
        rest_stops = [
            {
                "lat": element['lat'],
                "lon": element['lon'],
                "location": element.get('tags', {}).get('name', 'Rest Area'),
                "distance": 0.0
            }
            for element in rest_data['elements']
        ]
    except requests.RequestException as e:
        print(f"Overpass rest query failed: {e}")
        rest_stops = []

    for poi in fuel_stations + rest_stops:
        min_distance = float('inf')
        closest_miles = 0
        for i in range(len(geometry) - 1):
            lon1, lat1 = geometry[i]
            lon2, lat2 = geometry[i + 1]
            poi_lat, poi_lon = poi['lat'], poi['lon']
            seg_dist = haversine(lon1, lat1, poi_lon, poi_lat) + haversine(lon2, lat2, poi_lon, poi_lat)
            if seg_dist < min_distance:
                min_distance = seg_dist
                closest_miles = geometry_distances[i]
        poi['distance'] = closest_miles

    fuel_stations.sort(key=lambda x: x['distance'])
    rest_stops.sort(key=lambda x: x['distance'])

    # Step 2: Check if start and pickup points are the same
    is_start_pickup_same = (
        abs(start_coords['latitude'] - pickup_coords['latitude']) < 0.0001 and
        abs(start_coords['longitude'] - pickup_coords['longitude']) < 0.0001
    )

    # Step 3: Add the start point (and pickup point if they are the same)
    if is_start_pickup_same:
        # Combine Start and Pickup into one stop
        stops.append({
            "location": "Start/Pickup",
            "time": time_in_day,
            "duty_status": "on_duty_not_driving",
            "duration": 1,  # Keep the 1-hour duration for pickup activities
            "lat": start_coords['latitude'],
            "lon": start_coords['longitude'],
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
            "lat": start_coords['latitude'],
            "lon": start_coords['longitude'],
            "miles_traveled": current_miles
        })

        # Calculate distance to pickup and add pickup stop
        pickup_index = None
        for i, coord in enumerate(geometry):
            if abs(coord[0] - pickup_coords['longitude']) < 0.0001 and abs(coord[1] - pickup_coords['latitude']) < 0.0001:
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
            "lat": pickup_coords['latitude'],
            "lon": pickup_coords['longitude'],
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
    
    import pdb;pdb.set_trace()

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
        "lat": end_coords['latitude'],
        "lon": end_coords['longitude'],
        "miles_traveled": current_miles
    })
    total_on_duty_hours += 1

    trip_data = {"stops": stops, "total_days": day, "total_on_duty_hours": total_on_duty_hours}
    log_sheets = generate_eld_logs(trip_data)

    route =  {
                "distance_miles": distance_miles,
                "duration_hours": duration,
                "geometry": geometry,
                "stops": trip_data["stops"]
            }

    trip = Trip.objects.get(id=trip_id)
    trip.route_data = route
    trip.route_data = log_sheets
    trip.save()



    return trip_data
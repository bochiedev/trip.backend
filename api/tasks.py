# myapp/tasks.py
from celery import shared_task
from api.helpers.trip_planner import find_nearest_midpoint, get_coords_at_distance, get_overpass_data_sync, haversine,generate_eld_logs, preprocess_geometry
from api.models import Trip


@shared_task
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
        abs(start_coords['latitude'] - pickup_coords['latitude']) < 0.0001 and
        abs(start_coords['longitude'] - pickup_coords['longitude']) < 0.0001
    )

    #Add the start point with a pre-trip inspection
    if is_start_pickup_same:
        stops.append({
            "location": "Start/Pickup",
            "activity": "Pre-trip & TI",
            "time": time_in_day,
            "duty_status": "on_duty_not_driving",
            "duration": pre_trip_duration,
            "lat": start_coords['latitude'],
            "lon": start_coords['longitude'],
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
            "lat": start_coords['latitude'],
            "lon": start_coords['longitude'],
            "miles_traveled": current_miles
        })
        total_on_duty_hours += pre_trip_duration
        time_in_day += pre_trip_duration
        current_window_hours += pre_trip_duration

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
            "activity": "Loading",
            "time": time_in_day,
            "duty_status": "on_duty_not_driving",
            "duration": loading_duration,
            "lat": pickup_coords['latitude'],
            "lon": pickup_coords['longitude'],
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
        "lat": end_coords['latitude'],
        "lon": end_coords['longitude'],
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
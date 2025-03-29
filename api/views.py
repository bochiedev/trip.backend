from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.authtoken.models import Token
from api.tasks import calculate_trip
from api.helpers.trip_planner import calculate_trip as calculate_trip_data
from .models import Trip
from django.contrib.auth import authenticate
from .serializers import UserSerializer, TripSerializer
import requests
import base64
import io
from PIL import Image
import logging
from rest_framework.throttling import AnonRateThrottle


logger = logging.getLogger(__name__)



class LoginView(APIView):
    def post(self, request):
        driver_number = request.data.get("driver_number")
        password = request.data.get("password")
        user = authenticate(request, username=driver_number, password=password)
        if user:
            token, created = Token.objects.get_or_create(user=user)
            return Response({
                "token": token.key,
                "user": UserSerializer(user).data
            })
        return Response({"error": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED)
    
class UserProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        serializer = UserSerializer(user)
        return Response(serializer.data)

    def put(self, request):
        user = request.user
        data = request.data.copy()

        # Handle profile picture if a file is uploaded
        if 'profile_picture' in request.FILES:
            file = request.FILES['profile_picture']
            try:
                
                img = Image.open(file)
                img.verify()  
                img = Image.open(file)

                # Convert the image to Base64
                buffered = io.BytesIO()
                img.save(buffered, format=img.format if img.format else "PNG")
                img_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")

                # Add the data URI prefix
                mime_type = f"image/{img.format.lower() if img.format else 'png'}"
                data['profile_picture'] = f"data:{mime_type};base64,{img_base64}"
            except Exception as e:
                return Response({"profile_picture": f"Invalid image file: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

        serializer = UserSerializer(user, data=data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class TripHistoryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        day = request.query_params.get("day")
        trips = Trip.objects.filter(user=request.user).order_by('-created_at')
        if day:
            trips = trips.filter(created_at__date=day)
        serializer = TripSerializer(trips, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

class TripPlannerView(APIView):
    permission_classes = [IsAuthenticated]
    ors_api_key = settings.ORS_API_KEY
    ors_url = settings.ORS_URL

    def post(self, request):
        current_location = request.data.get("current_location")
        pickup_location = request.data.get("pickup_location")
        dropoff_location = request.data.get("dropoff_location")
        current_cycle_hours = float(request.data.get("current_cycle_hours", 0))


        current_coords = {"latitude": current_location['latitude'], "longitude": current_location['longitude']}
        pickup_coords =  {"latitude":pickup_location['latitude'], "longitude": pickup_location['longitude'] }
        dropoff_coords = {"latitude": dropoff_location['latitude'], "longitude": dropoff_location['longitude'] }

        coords = [
            [current_coords['longitude'], current_coords['latitude']],
            [pickup_coords['longitude'], pickup_coords['latitude']],
            [dropoff_coords['longitude'], dropoff_coords['latitude']]
        ]
        

        headers = {"Authorization": self.ors_api_key}
        body = {"coordinates": coords}
        response = requests.post(self.ors_url, json=body, headers=headers)
        route_data = response.json()

        distance = route_data["features"][0]["properties"]["summary"]["distance"] / 1000
        duration = route_data["features"][0]["properties"]["summary"]["duration"] / 3600
        geometry = route_data["features"][0]["geometry"]["coordinates"]

        trip = Trip.objects.create(
            user=request.user,
            current_location=current_location,
            pickup_location=pickup_location,
            dropoff_location=dropoff_location,
            current_cycle_hours=current_cycle_hours,
        )
        calculate_trip.delay(trip.id, distance, duration, current_cycle_hours, geometry, pickup_coords, current_coords, dropoff_coords)

        trip = TripSerializer(trip)

        response_data = {
            "message": "Trip Created Succesfully",
            "data": trip.data
        }

        return Response(response_data, status=status.HTTP_201_CREATED)
    


class RouteDataView(APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        try:
            trip_id = request.query_params.get("trip_id")
            update = request.query_params.get("update")

            if update:
                trip = Trip.objects.get(id=trip_id)
            else:
                trip = Trip.objects.get(id=trip_id, route_data__isnull=True, log_sheets__isnull=True)


            current_location = trip.current_location
            pickup_location = trip.pickup_location
            dropoff_location = trip.dropoff_location
            current_cycle_hours = float(trip.current_cycle_hours)

            # Validate that all locations and their coordinates are provided
            if not all([current_location, pickup_location, dropoff_location]):
                logger.error("One or more locations are missing")
                return Response({"error": "All locations are required"}, status=400)

            required_fields = ["name", "latitude", "longitude"]
            for loc, loc_name in [
                (current_location, "current_location"),
                (pickup_location, "pickup_location"),
                (dropoff_location, "dropoff_location"),
            ]:
                if not all(field in loc for field in required_fields):
                    logger.error(f"Missing fields in {loc_name}: {loc}")
                    return Response({"error": f"Missing fields in {loc_name}"}, status=400)

            # Create location objects with coordinates
            current_coords = type('Location', (), {
                'latitude': float(current_location["latitude"]),
                'longitude': float(current_location["longitude"])
            })
            pickup_coords = type('Location', (), {
                'latitude': float(pickup_location["latitude"]),
                'longitude': float(pickup_location["longitude"])
            })
            dropoff_coords = type('Location', (), {
                'latitude': float(dropoff_location["latitude"]),
                'longitude': float(dropoff_location["longitude"])
            })

            logger.info("Requesting route from OpenRouteService")
            ors_api_key = settings.ORS_API_KEY
            coords = [
                [current_coords.longitude, current_coords.latitude],
                [pickup_coords.longitude, pickup_coords.latitude],
                [dropoff_coords.longitude, dropoff_coords.latitude]
            ]


            ors_url = settings.ORS_URL
            headers = {"Authorization": ors_api_key}
            body = {"coordinates": coords}
            try:
                response = requests.post(ors_url, json=body, headers=headers, timeout=10)
                response.raise_for_status()
                route_data = response.json()
                logger.info("Successfully received route data from ORS")
            except requests.exceptions.RequestException as e:
                logger.error(f"ORS request failed: {str(e)}")
                return Response({"error": f"Failed to fetch route from ORS: {str(e)}"}, status=500)

            distance = route_data["features"][0]["properties"]["summary"]["distance"] / 1000
            duration = route_data["features"][0]["properties"]["summary"]["duration"] / 3600
            geometry = route_data["features"][0]["geometry"]["coordinates"]

            logger.info("Calculating trip stops")
            try:
                calculate_trip_data(trip_id, distance, duration, current_cycle_hours, geometry, pickup_coords, current_coords, dropoff_coords)
                logger.info("Trip stops calculated successfully")
            except Exception as e:
                logger.error(f"calculate_trip failed: {str(e)}")
                return Response({"error": f"Failed to calculate trip: {str(e)}"}, status=500)


            response_data = {
                "message": "Route Data creation initiated successfully"
            }

            logger.info("Returning response to client")
            return Response(response_data, status=status.HTTP_200_OK)
        
        except Trip.DoesNotExist:
            return Response("No Trip found with that ID", status=status.HTTP_404_NOT_FOUND)

        except Exception as e:
            return Response(f"Error Occured {str(e)}", status=status.HTTP_400_BAD_REQUEST)


class LocationView(APIView):
    throttle_classes = [AnonRateThrottle]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        query = request.query_params.get("q", "")
        if not query:
            return Response({"error": "Query parameter 'q' is required"}, status=400)

        logger.info(f"Fetching autocomplete suggestions for query: {query}")

        nominatim_url = "https://nominatim.openstreetmap.org/search"
        params = {
            "q": query,
            "format": "json",
            "limit": 10,
            "addressdetails": 1,
            "countrycodes": "us",
        }
        headers = {
            "User-Agent": "eld_app"
        }

        try:
            response = requests.get(nominatim_url, params=params, headers=headers, timeout=5)
            response.raise_for_status()
            results = response.json()

            suggestions = [
                {
                    "name": result["display_name"],
                    "latitude": float(result["lat"]),
                    "longitude": float(result["lon"]),
                }
                for result in results
            ]

            logger.info(f"Found {len(suggestions)} town/city suggestions for query: {query}")
            return Response(suggestions)
        except requests.exceptions.RequestException as e:
            logger.error(f"Nominatim autocomplete request failed: {str(e)}")
            return Response({"error": f"Failed to fetch location suggestions: {str(e)}"}, status=500)
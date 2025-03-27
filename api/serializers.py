from rest_framework import serializers
from .models import User, Trip
import base64

class UserSerializer(serializers.ModelSerializer):
    profile_picture = serializers.CharField(allow_blank=True, allow_null=True, required=False)

    class Meta:
        model = User
        fields = ['id', 'driver_number', 'first_name', 'last_name', 'trailer_number', 'truck_number', 'profile_picture']

    def validate_profile_picture(self, value):
        if value and not value.startswith("data:image"):
            try:
                base64.b64decode(value.split(",")[1] if "," in value else value)
            except Exception:
                raise serializers.ValidationError("Invalid Base64 string for profile picture.")
        return value

class TripSerializer(serializers.ModelSerializer):
    class Meta:
        model = Trip
        fields = ['id', 'current_location', 'pickup_location', 'dropoff_location', 'current_cycle_hours', 'created_at', 'updated_at', 'route_data', 'log_sheets']

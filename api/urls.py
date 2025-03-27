from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from .views import UserProfileView, TripHistoryView, TripPlannerView, LocationView, RouteDataView

urlpatterns = [
    path("login/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("profile/", UserProfileView.as_view(), name="profile"),
    path("trip-history/", TripHistoryView.as_view(), name="trip_history"),
    path("plan-trip/", TripPlannerView.as_view(), name="plan_trip"),
    path('locations/', LocationView.as_view(), name='location'),
    path('create-route-data/', RouteDataView.as_view(), name='create_route_data'),

]



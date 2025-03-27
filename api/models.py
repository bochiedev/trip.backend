from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models

class UserManager(BaseUserManager):
    def create_user(self, driver_number, password=None, **extra_fields):
        if not driver_number:
            raise ValueError("The Driver Number must be set")
        user = self.model(driver_number=driver_number, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, driver_number, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')

        return self.create_user(driver_number, password, **extra_fields)

class User(AbstractBaseUser, PermissionsMixin):
    driver_number = models.CharField(max_length=50, unique=True)
    first_name = models.CharField(max_length=50, blank=True)
    last_name = models.CharField(max_length=50, blank=True)
    trailer_number = models.CharField(max_length=50, blank=True)
    truck_number = models.CharField(max_length=50, blank=True)
    profile_picture = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    objects = UserManager()

    USERNAME_FIELD = 'driver_number'
    REQUIRED_FIELDS = []

    def __str__(self):
        return self.driver_number

class Trip(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='trips')
    current_location = models.JSONField()
    pickup_location = models.JSONField()
    dropoff_location = models.JSONField()
    current_cycle_hours = models.FloatField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    route_data = models.JSONField(null=True, blank=False)
    log_sheets = models.JSONField(null=True, blank=False)

    def __str__(self):
        return f"Trip for {self.user.driver_number} on {self.created_at}"

from django.contrib import admin
from .models import Job, AspNetUser, Sector

@admin.register(AspNetUser)
class AspNetUserAdmin(admin.ModelAdmin):
    list_display = ("email", "first_name", "last_name", "phone_number", "email_confirmed", "created_at")
    search_fields = ("email", "first_name", "last_name", "phone_number")
    list_filter = ("email_confirmed",)
    readonly_fields = ("id", "created_at")
    
@admin.register(Sector)
class SectorAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "created_at")
    search_fields = ("name", "slug")
    list_filter = ("is_active",)
    readonly_fields = ("id", "created_at")

@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ("title", "company", "location", "status", "is_active", "sector", "posted_by", "published_at")
    list_filter = ("status", "is_active", "sector", "source_name")
    search_fields = ("title", "company", "location")
    readonly_fields = ("id", "created_at")

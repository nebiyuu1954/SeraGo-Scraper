from django.contrib import admin
from .models import Job, AspNetUser, Sector, TalentProfile, RecruiterProfile, JobApplication, SavedJob

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

@admin.register(TalentProfile)
class TalentProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "headline", "experience_level", "years_of_experience", "created_at")
    search_fields = ("user__email", "user__first_name", "headline")
    readonly_fields = ("user", "created_at", "updated_at")

@admin.register(RecruiterProfile)
class RecruiterProfileAdmin(admin.ModelAdmin):
    list_display = ("company_name", "industry", "company_size", "user", "created_at")
    search_fields = ("company_name", "industry", "user__email")
    readonly_fields = ("user", "created_at", "updated_at")

@admin.register(JobApplication)
class JobApplicationAdmin(admin.ModelAdmin):
    list_display = ("user", "job", "status", "applied_at")
    list_filter = ("status",)
    search_fields = ("user__email", "job__title")
    readonly_fields = ("id", "applied_at")

@admin.register(SavedJob)
class SavedJobAdmin(admin.ModelAdmin):
    list_display = ("user", "title", "company", "saved_at")
    search_fields = ("user__email", "title", "company")
    readonly_fields = ("id", "saved_at")

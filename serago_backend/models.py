from django.db import models
from django.utils.translation import gettext_lazy as _

class AspNetUser(models.Model):
    id = models.CharField(max_length=450, primary_key=True)
    user_name = models.CharField(max_length=256, null=True, blank=True, db_column="UserName")
    normalized_user_name = models.CharField(max_length=256, null=True, blank=True, db_column="NormalizedUserName")
    email = models.CharField(max_length=256, null=True, blank=True, db_column="Email")
    normalized_email = models.CharField(max_length=256, null=True, blank=True, db_column="NormalizedEmail")
    email_confirmed = models.BooleanField(db_column="EmailConfirmed")
    phone_number = models.CharField(max_length=256, null=True, blank=True, db_column="PhoneNumber")
    first_name = models.CharField(max_length=256, null=True, blank=True, db_column="FirstName")
    last_name = models.CharField(max_length=256, null=True, blank=True, db_column="LastName")
    created_at = models.DateTimeField(db_column="CreatedAt")
    
    class Meta:
        managed = False
        db_table = 'AspNetUsers'
        verbose_name = 'User (.NET)'
        verbose_name_plural = 'Users (.NET)'

    def __str__(self):
        return self.email or self.user_name or self.id

class Sector(models.Model):
    id = models.UUIDField(primary_key=True)
    name = models.CharField(max_length=256, db_column="Name")
    slug = models.CharField(max_length=256, db_column="Slug")
    is_active = models.BooleanField(db_column="IsActive")
    created_at = models.DateTimeField(db_column="CreatedAt")

    class Meta:
        managed = False
        db_table = 'Sectors'
        verbose_name = 'Sector (.NET)'
        verbose_name_plural = 'Sectors (.NET)'

    def __str__(self):
        return self.name

class Job(models.Model):
    id = models.UUIDField(primary_key=True)
    title = models.CharField(max_length=256, db_column="Title")
    company = models.CharField(max_length=256, db_column="Company")
    location = models.CharField(max_length=256, db_column="Location")
    url = models.TextField(db_column="Url", null=True, blank=True)
    status = models.IntegerField(db_column="Status")
    is_active = models.BooleanField(db_column="IsActive")
    published_at = models.DateTimeField(db_column="PublishedAt", null=True, blank=True)
    created_at = models.DateTimeField(db_column="CreatedAt")
    source_name = models.CharField(max_length=256, db_column="SourceName", null=True, blank=True)
    external_id = models.CharField(max_length=256, db_column="ExternalId", null=True, blank=True)
    sector_name = models.CharField(max_length=256, db_column="SectorName", null=True, blank=True)

    # Note: defining sector as a ForeignKey to Sector allows the Django admin to create a dropdown
    sector = models.ForeignKey(Sector, on_delete=models.DO_NOTHING, db_column="SectorId", null=True, blank=True)
    posted_by = models.ForeignKey(AspNetUser, on_delete=models.DO_NOTHING, db_column="PostedByUserId", null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'Jobs'
        verbose_name = 'Job (.NET)'
        verbose_name_plural = 'Jobs (.NET)'

    def __str__(self):
        return self.title

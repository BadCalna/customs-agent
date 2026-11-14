from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import TaskAssignment, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("业务角色", {"fields": ("role", "display_name")}),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ("业务角色", {"fields": ("role", "display_name")}),
    )
    list_display = ("username", "display_name", "role", "is_staff", "is_active")
    list_filter = ("role", "is_staff", "is_active")


@admin.register(TaskAssignment)
class TaskAssignmentAdmin(admin.ModelAdmin):
    list_display = ("task", "user", "role", "created_at")
    list_filter = ("role",)
    search_fields = ("task__title", "user__username")

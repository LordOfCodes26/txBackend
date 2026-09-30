from django.db import models
from django.utils import timezone


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SoftDeleteQuerySet(models.QuerySet):
    def alive(self):
        return self.filter(deleted_at__isnull=True)

    def dead(self):
        return self.filter(deleted_at__isnull=False)


class AliveManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class SoftDeleteModel(models.Model):
    """`objects` hides soft-deleted rows; `all_objects` sees everything.

    Unique constraints on soft-deletable models should be partial
    (`condition=Q(deleted_at__isnull=True)`) so a deleted row does not block reuse.
    """

    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects = AliveManager()
    all_objects = SoftDeleteQuerySet.as_manager()

    class Meta:
        abstract = True

    def soft_delete(self):
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at"])


class AppendOnlyModel(models.Model):
    """Rows can be inserted but never updated or deleted.

    This is the application-level guard; pair it with `append_only_trigger()` in the
    model's migration so the database enforces the same rule for raw SQL and bulk ops.
    """

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError(f"{type(self).__name__} is append-only and cannot be updated.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError(f"{type(self).__name__} is append-only and cannot be deleted.")

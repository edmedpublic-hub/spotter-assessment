from django.db import models


class Station(models.Model):
    """A fuel station from the employer-supplied CSV.

    Only stations successfully joined to the Census Gazetteer Places file
    are stored (6,207 of 6,738 deduplicated records). Coordinates are
    city-level centroids, not forecourt locations — see README.
    """

    opis_id = models.CharField(
        max_length=32,
        unique=True,
        help_text="OPIS Truckstop ID from the source CSV, used as the natural key.",
    )
    name = models.CharField(max_length=255)
    city = models.CharField(max_length=128)
    state = models.CharField(
        max_length=2,
        help_text="USPS two-letter state abbreviation (Canadian records excluded).",
    )
    lat = models.FloatField()
    lon = models.FloatField()
    retail_price = models.DecimalField(
        max_digits=10,
        decimal_places=8,
        help_text="Retail price per gallon in USD, as supplied in the CSV.",
    )

    class Meta:
        ordering = ["opis_id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) @ ${self.retail_price}"
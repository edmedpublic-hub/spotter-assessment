from rest_framework import serializers


class RouteRequestSerializer(serializers.Serializer):
    """Validates a POST /api/v1/route/ body.

    Both fields must be city + state strings, e.g. 'Kansas City, MO'.
    """

    origin = serializers.CharField(
        max_length=120,
        trim_whitespace=True,
        help_text="Starting location in the USA, e.g. 'Kansas City, MO'.",
    )
    destination = serializers.CharField(
        max_length=120,
        trim_whitespace=True,
        help_text="Finishing location in the USA, e.g. 'Los Angeles, CA'.",
    )

    def validate_origin(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("origin cannot be blank.")
        return value

    def validate_destination(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("destination cannot be blank.")
        return value
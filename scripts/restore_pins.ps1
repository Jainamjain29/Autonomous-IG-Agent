# Restores the known-working Gemini stack compatible with AppLocker / Smart App Control.
# Dependency set is intentionally inconsistent on grpcio to satisfy Code Integrity.
uv pip install --no-deps grpcio==1.60.0 grpcio-status==1.71.2 protobuf==5.29.6 google-api-core==2.33.0

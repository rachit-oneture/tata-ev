"""Plain-language meanings for standard OCPP 1.6 values (not vendor codes).

Source: the OCPP 1.6 specification (StatusNotification errorCode, status and
StopTransaction reason). Vendor codes are never explained here; they come only
from the catalogue.
"""

OCPP_ERROR_CODES = {
    "ConnectorLockFailure": "the connector could not lock or unlock",
    "EVCommunicationError": "communication with the vehicle failed",
    "GroundFailure": "the ground-fault protection tripped",
    "HighTemperature": "the charger is too hot inside",
    "InternalError": "a fault inside the charger's hardware or software",
    "LocalListConflict": "a conflict in the charger's local authorization list",
    "OtherError": "an error the charger did not classify further",
    "OverCurrentFailure": "current went above the allowed level",
    "OverVoltage": "voltage went above the acceptable level",
    "PowerMeterFailure": "the charger could not read its power meter",
    "PowerSwitchFailure": "the charger could not switch power on or off",
    "ReaderFailure": "the card reader failed",
    "ResetFailure": "the charger could not reset",
    "UnderVoltage": "voltage dropped below the acceptable level",
    "WeakSignal": "the charger's wireless connection signal is weak",
}

STATUS_WORDS = {
    "Available": "ready and idle",
    "Preparing": "preparing: a vehicle is plugged in or a session is being set up",
    "Charging": "charging",
    "SuspendedEV": "paused by the vehicle",
    "SuspendedEVSE": "paused by the charger",
    "Finishing": "finishing the session",
    "Reserved": "reserved",
    "Unavailable": "out of service",
    "Faulted": "in a fault state",
}

# Values that describe state, never a fault code in their own right.
NON_CODE_VALUES = set(STATUS_WORDS) | {"NoError", "null", "None", ""}

STOP_REASONS = {
    "Local": "stopped at the charger",
    "Remote": "stopped by a remote command",
    "EVDisconnected": "the vehicle disconnected or its communication dropped",
    "Other": "stopped for a reason the charger did not specify",
    "PowerLoss": "the charger lost power",
    "EmergencyStop": "the emergency stop was used",
    "DeAuthorized": "authorization was withdrawn",
    "HardReset": "the charger did a hard reset",
    "SoftReset": "the charger did a soft reset",
    "Reboot": "the charger rebooted",
    "UnlockCommand": "the connector was unlocked by command",
}
# Stop reasons that are an ordinary end of a session.
NORMAL_STOP_REASONS = {"Local", "Remote"}

SOURCE = "OCPP 1.6 standard"

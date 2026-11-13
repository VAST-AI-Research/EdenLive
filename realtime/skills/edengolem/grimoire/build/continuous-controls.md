# Continuous and automatic control

Outcome: input controls the intended motion with predictable constraints and visible consequences.
Use when state evolves through movement, steering or repeated time integration. Discrete actions
can coexist; read [discrete interactions](discrete-interactions.md) only for those capabilities.

## Workflow

Read exact module signatures in the selected [motion catalog](../../assets/catalogs/motion.md).

| Required behavior | Asset candidates | Verify only when applicable |
| --- | --- | --- |
| World-relative character movement | WorldCardinalCharacterMotionController / WorldTargetCharacterMotionController | Screen directions, arrival or release, support |
| Heading or mouse-relative character movement | HeadingRelativeCharacterMotionController / MouseLookCharacterMotionController | Turning versus strafing, facing, look units/sign |
| Camera-relative third-person orbit | CameraRelativeCharacterMotionController + OrbitFollowCameraRig | WASD in the camera frame, persistent yaw after look release, optional lock yaw; the actor need not be screen-centered |
| Ground driving | ArcadeCarMotionController; dynamic car stack if wheel physics matters | Steering at speed, throttle/brake/reverse, contact |
| Free flight | AirplaneMotionController / GeneralVehicleMotionController | Chosen pitch/yaw/roll mapping, altitude and travel; keyboard or pointer as specified |
| Automatic forward motion | LaneRunnerMotionController / FlapFlightMotionController | Lane change, impulse or dodge timing while forward motion continues |
| Automatic vertical motion | AutoHopMotionController | Air steering and supported landing; no artificial stop requirement |
| Indirect control | PlateTiltController + TiltRollMotionController; PaddleBallModel for paddle contact | Input affects board/paddle, then ball trajectory; no character gravity by default |

Choose fixed, position-following, pose-following, orbit or first-person framing from what the
player must see. An enum does not dictate a camera. Screen axes may differ from world axes; use
WorldBasis and measure the advertised relationship rather than forcing all cameras into one
coordinate convention. For character support, mouse-look and camera-relative orbit APIs use
[player integration](../patterns/player-controller.md).

## Verify and record

A stopping check applies to player-controlled stopping. Automatic motion instead checks timing and
steering authority; inertial vehicles check predictable braking distance. A constrained paddle needs
only its playable axis. Camera speed limits must come from the game, not a library-wide number.

For jump/support mechanics, verify takeoff, landing, falling and any advertised extra jumps. Use
trajectory arithmetic only for the actual movement model; automatic bounces, impulses or moving
supports may require simulation. Coyote time and buffering are optional; reset each on its defined
edge. For solids, use the conditional [collision reference](../patterns/box-collision.md).
Record the actual control mapping, tested boundaries and observations with the active stage's
evidence. For lost presses, repeated actions or stale input, use the
[input diagnostics](../review/input-contract.md#troubleshooting).

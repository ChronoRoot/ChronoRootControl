# ChronoRoot 2.0: an open AI-powered platform for 2D temporal plant phenotyping

Gaggion, N., Boccardo, N.A., Bonazzola, R., et al. — GigaScience, Volume 15, January 2026. [doi:10.1093/gigascience/giag018](https://doi.org/10.1093/gigascience/giag018)

## Original ChronoRoot publication (2021)

*ChronoRoot: High-throughput phenotyping by deep learning reveals novel temporal parameters of plant root system architecture*

Nicolás Gaggion¹, Federico Ariel², Vladimir Daric³, Éric Lambert³, Simon Legendre³, Thomas Roulé³, Alejandra Camoirano², Diego Milone¹, Martin Crespi³, Thomas Blein³, Enzo Ferrante¹

[doi:10.1093/gigascience/giab052](https://doi.org/10.1093/gigascience/giab052)

¹ Research Institute for Signals, Systems and Computational Intelligence (sinc(i)), FICH-UNL, CONICET, Ciudad Universitaria UNL, Santa Fe, Argentina.\
² Instituto de Agrobiotecnología del Litoral (IAL), CONICET, FBCB, Universidad Nacional del Litoral, Colectora Ruta Nacional 168 km 0, Santa Fe, Argentina.\
³ Institute of Plant Sciences Paris-Saclay (IPS2), CNRS, INRA, University Paris-Saclay and University of Paris Bâtiment 630, 91192 Gif sur Yvette, France.

![ChronoRoot workflow](app/static/img/workflow_resunetds.png)

# ChronoRootControl

A web interface to control the ChronoRoot module designed
for image-based plant phenotyping.

**ChronoRootControl v2.0.0** updates the platform to **Debian Trixie** (Raspberry Pi OS Lite, 64-bit) with **Picamera2**, adds support for **Raspberry Pi Camera Module v3** (including dedicated capture profiles and tuning), and introduces a new [**REST API**](app/doc/06_api.md) for fleet / master-controller integration.

## ChronoRoot module description

The ChronoRoot device is an affordable and modular imaging system based on 3D-printed
and laser cut pieces and off-the-shelf electronics. Each module runs on a Raspberry Pi
with Raspberry Pi OS Lite (Trixie) and Raspberry Pi Camera Modules (v2 or v3,
auto-detected via Picamera2). Two hardware configurations are supported:

- **Single-camera module (Raspberry Pi Zero 2 W, or 3B/3B+ without multiplexer):**
  one camera connected directly to the Pi. This is the typical setup for
  Raspberry Pi Zero 2 W.
- **Multi-camera module (Raspberry Pi 3B/3B+ with IVPort multiplexer):**
  up to four cameras on a single Pi.

See the [installation guide](app/doc/03_install.md) for setup details.

An array of infrared (IR) LED back-light sits behind each growth plate. In between
each camera and the corresponding IR array, there is a vertical 12 x 12 cm plate for
seedling growth, allowing automatic image acquisition repeatedly along the experiment
without any modification or movement of the imaging setup. A four-plate module is small
(62 x 36 x 20 cm) and can be placed in any growth chamber. The different parts of
the imaging setup (back-light, plate support and camera) can be positioned along
a horizontal double-rail to control the field of view of the camera and accurate lightning.
In addition, the camera can be moved vertically. ChronoRoot allows image
acquisition at a high temporal resolution (a set of pictures every minute). The
use of an IR back-light (850 nm) and optional long pass IR filters (> 830 nm)
allow acquiring images of the same quality independently from the light conditions
required for the experiment, during day and night.

Imaging is handled by Picamera2 / libcamera with configurable capture profiles
(see the [configuration guide](app/doc/04_configuration.md)).

Each module is connected to the network either by Wi-Fi or Ethernet cable. A web
interface allows the control of the device offering live feed of the cameras for
field of view and focus setup. The user can program the activation of cameras and
IR back-light, starting and ending dates, the time basis for picture acquisition,
and finally follow the progression of the experiment. The pictures are saved
directly on an external drive plugged on the Raspberry Pi. Once the experimental
setup is ready, each module is completely independent from the external
environment and the access to the network.

## ChronoRootControl documentation

Source files live under `app/doc/`. When the app is running, the same content is available in the browser at `/help/<section>`.

* [About & References](app/doc/01_about.md)
* [User Manual](app/doc/02_manual.md)
* [Installation Guide](app/doc/03_install.md)
* [Configuration Guide](app/doc/04_configuration.md)
* [App Architecture](app/doc/05_architecture.md)
* [REST API (Fleet)](app/doc/06_api.md)

# Image analysis

Software for image analysis in the [ChronoRoot repository](https://github.com/ngaggion/ChronoRoot/)

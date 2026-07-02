# Welcome to ChronoRoot

A web interface to control a ChronoRoot module for high-throughput temporal plant phenotyping.

## Module Description

A ChronoRoot module aims to take regular pictures of in-vitro plates of growing plants. The plates are classical square transparent plates (125mm x 125mm) positioned vertically in a support. In front of each plate, a camera is present that is controlled by the module (a Raspberry Pi).

To be able to follow root growth in all conditions, an IR backlight is positioned behind the plates. It allows the system to take pictures even during the night without disturbing the plants, and to eliminate reflections on the plates caused by the light of the growth chamber. The backlight is optional and can be toggled at the experiment level.

Each module is able to control up to 4 cameras simultaneously using a hardware multiplexer.

---

## About ChronoRootControl

**Copyright:** 2016-2026 IPS2  
**Version:** v2.0.0  
**Licence:** CeCILL v2.1 OR GNU GPL v3

### Contributors
* Thomas Blein
* Vladimir Daric
* Nicolás Gaggion

### Links
* [IPS2 Website](http://ips2.u-psud.fr)
* [ChronoRoot Main Website](https://chronoroot.github.io/)
* [ChronoRootControl Source Code](https://github.com/ChronoRoot/ChronoRootControl)
* [ChronoRoot Image Analysis Pipeline](https://github.com/ChronoRoot/ChronoRoot2)

---

## References

If you use ChronoRoot in your research, please cite the following publications:

### ChronoRoot 2.0 (2026)
**ChronoRoot 2.0: an open AI-powered platform for 2D temporal plant phenotyping**  
*Gaggion, N., Boccardo, N.A., Bonazzola, R., et al.*  
GigaScience, Volume 15, January 2026.  
[doi: 10.1093/gigascience/giag018](https://doi.org/10.1093/gigascience/giag018)

```bibtex
@article{10.1093/gigascience/giag018,
    author = {Gaggion, Nicolás and Boccardo, Noelia A and Bonazzola, Rodrigo and Legascue, María Florencia and Mammarella, María Florencia and Rodriguez, Florencia Sol and Aballay, Federico Emanuel and Catulo, Florencia Belén and Barrios, Andana and Santoro, Luciano J and Accavallo, Franco and Villarreal, Santiago Nahuel and Pereyra-Bistrain, Leonardo I and Benhamed, Moussa and Crespi, Martin and Ricardi, Martiniano María and Petrillo, Ezequiel and Blein, Thomas and Ariel, Federico and Ferrante, Enzo},
    title = {ChronoRoot 2.0: an open AI-powered platform for 2D temporal plant phenotyping},
    journal = {GigaScience},
    volume = {15},
    pages = {giag018},
    year = {2026},
    month = {01},
    issn = {2047-217X},
    doi = {10.1093/gigascience/giag018},
}
```

### ChronoRoot 1.0 (2021)
***ChronoRoot: High-throughput phenotyping by deep segmentation networks reveals novel temporal parameters of plant root system architecture***  
*Gaggion, N., Ariel, F., Daric, V., et al.*  
GigaScience, Volume 10, July 2021.
[doi: 10.1093/gigascience/giab052](https://doi.org/10.1093/gigascience/giab052)

```bibtex
@article{10.1093/gigascience/giab052,
    author = {Gaggion, Nicolás and Ariel, Federico and Daric, Vladimir and Lambert, Éric and Legendre, Simon and Roulé, Thomas and Camoirano, Alejandra and Milone, Diego H and Crespi, Martin and Blein, Thomas and Ferrante, Enzo},
    title = {ChronoRoot: High-throughput phenotyping by deep segmentation networks reveals novel temporal parameters of plant root system architecture},
    journal = {GigaScience},
    volume = {10},
    number = {7},
    pages = {giab052},
    year = {2021},
    month = {07},
    issn = {2047-217X},
    doi = {10.1093/gigascience/giab052},
}
```
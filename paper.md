---
title: The Spectra Pointer: A Pointer Database for Stellar Spectroscopy
tags:
  - Python
  - astronomy
  - astrophysics
  - spectroscopy
  - Gaia
authors:
  - name: Zachary Way
    orcid: 0000-0003-0179-9662
    affiliation: 1
affiliations:
  - name: Department of Physics and Astronomy, Georgia State University, Atlanta, GA 30303, USA
    index: 1
date: <TODO: submission date>
bibliography: paper.bib
---

# Summary

A cross-match database and search tool that unifies stellar spectroscopy holdings across more than 50 independent astronomical archives (ESO, SDSS, Gaia, LAMOST, Keck/KOA, Gemini, and more) behind a single Gaia DR3 `source_id` lookup.


# Statement of need

Since the invention of the charge coupled device (CCD), astronomical data taken at observatories has been stored digitally. It is common for ground- and space-based spectroscopic instruments to publish their data publicly, however these are held under many different conventions — different identifier schemes, different metadata fields, different levels of positional precision, some with public TAP/VO services and some without. As more spectroscopic data is observed, it becomes intractable for an individual researcher to find out if a particular star has been observed or by which observatories. This information is necessary for creating novel research, combing through archival data, and writing proposals for time on large telescopes with limited budgets.

The Spectra Pointer solves this by running an independent sync process per archive (57 are currently implemented) that discovers spectroscopic observations and cross-matches each one to a canonical identifier, either the Gaia DR3 `source_id` [@gaiadr3] or from the Bright Stars Catalog [@hoffleit1991bsc]. First, stars are matched by a named identifier if the archive publishes one (via the SIMBAD database [@wenger2000simbad]. If the name cannot be resolved, the code falls back to a positional match (via a PostgreSQL q3c extension [@koposov2006q3c]). The result is a single Postgres table (spectroscopy_holdings) that a researcher can query by Gaia `source_id` or common name to get a consolidated, deduplicated list of every archive holding a spectrum of that star, along with pointers back to the original data in each home archive. A public search webapp (built on this database) is deployed at https://spectra-pointer-997472993697.us-central1.run.app. This software is developed to be reusable infrastructure for anybody, using no proprietary access and minimal personal keys.

# State of the Field

CDS’ VizieR [@ochsenbein2000vizier] tool allows a user to search through associated data to look for spectra. However, this tool is based on their internal table matching. The Spectra Pointer, in contrast, directly searches each archive, matches the holding to a star internally, and aggregates the spectroscopic holdings. This method seeks out data which may not be documented in a published paper and effectively casts a much wider net for public data.

# Software Design

The software can be cleanly split into two sections, the cross-match database and The Spectra Pointer webapp.

## The Cross-Match Database

Matching stellar data to a particular source is a notoriously difficult problem in astronomy. As new technology is developed our view of the sky becomes sharper and deeper, allowing unresolved or dim objects to be tracked. Furthermore, stars do not stay in one place and have observable, proper motion across the sky. In The Spectra Pointer these issues are accounted for by 1) trusting what observers themselves wrote down as the target and 2) anchoring our cross match to the highest resolution and deepest survey of stars to date Gaia DR3.

The cross match process works as follows:
 - An online archive is scanned for spectroscopy holdings
 - Each holding is attempted to be resolved in the following order and added to the `spectroscopy_holdings` table
   - A name match to the SIMBAD database which already has a mapped Gaia DR3 `source_id`
   - An easy, proper motion-propagated coordinate match to a 1 arcsecond radius to the Gaia archive. This is constrained enough to limit false-positives (although they do exist)
   - A lower quality, 1 arcminute search with underlying match logic (e.g. which source is brightest) is run on the Gaia archive
   - If the above fail, the holdings is stored as “skipped”
 - Once the holding is tied to a `source_id`, the table `stars` is checked to see if a star already exists. If not, the row is created. If it does exists, `star_id` is updated for the holding.

This process is completed intermittently through the `sync` method. Each archive has a running cursor which keeps track of how far the sync has reached. When the sync is run, each archive is matched and added into the database.

Prioritizing the archive’s named target allows the database to be based off of the Gaia archive[^1] while still balancing issues with coordinates that arise during observations. After matching all spectroscopy holdings, the database is less than a few GB large.

[^1]: About 3,000 stars are too bright for Gaia, in these cases the Bright Star Catalog (BSC) is used as a base. This catalog fully covers the missing stars.

## The Spectra Pointer Webapp

The Spectra Pointer is designed to be as accesible as possible while limiting the total cost on cloud services. The webapp is built using Flask and consists of a simple search page with some other interesting plots and features. In order to limit API calls, the Postgres database is periodically exported to static `parquet` files. The Spectra Pointer accesses these files using DuckDB's httpfs extension. This architecture, while convoluted, allows the data to be hosted on Georgia State’s public server while the app is accessible using Google Cloud’s web services.

# Research Impact Statement

This work will provide stellar astronomers with a tool that is both broad and deep. With more than 16.8 million stars tracked, an astronomer who studies a particular star can now search immediately to see if their source has been observed. This breadth of coverage is relevant for time-domain astronomy, where a source can be characterized without follow-up, as well as for inter-archival comparison and validation.

The depth of the data is staggering. At the time of writing Vega is the most observed star, with more than 35,000 spectra taken from 1978 until today. AU Microscopii has the most wavelength coverage, with overlapping data taken from the x-ray all the way to the far infrared. The Spectra Pointer allows a user to, at a glance, see all the available spectra for a particular source and enrich the research done on these sources.

# AI usage disclosure

Claude’s Sonnet 5 was used to develop the software throughout the codebase. The author has read through each line of code to verify correctness. Sonnet 5 was also used to generate some of the text in this paper as well as formatting and copy-editing. However, the writing can be largely credited to the author.


# Acknowledgements

This research has made use of the SIMBAD database,
operated at CDS, Strasbourg, France

For their feedback during development I would like to acknowledge Jamie Tayar, Kayvon Sharifi, Colin Kane, Akshat Chaturvedi, Mahir Patel, Doug Gies, Russel White, Thomas Rivinius, Dietrich Baade, Ilija Medan, and my PhD advisor Sébastien Lépine.

I would like to send my heartfelt gratitude to Chad Gottuso for designing the logo and his support over the years. Lastly, I would like to thank Gunner, the only dog in the Way family to ever point.

# References
2000,A&AS,143,9 , "The SIMBAD astronomical database", Wenger et al.

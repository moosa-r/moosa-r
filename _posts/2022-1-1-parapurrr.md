---
title: "parapurr: Do purrr in Parallel (Alpha version)"
alt_title: parapurr
sub_title: "Do purrr in Parallel (Alpha version)"
permalink: /projects/parapurrr
introduction:
image:
  path: /assets/images/header_parapurrr.jpg
  thumbnail: /assets/images/header_parapurrr.jpg
  alt: A collage illustration depicts a man performing a magic trick by turning a single cat into numerous cats, symbolizing the parapurrr R package's objective to run purrr package's function using multiple CPU cores.
categories:
  - "R Packages"
type: projects
tags:
  - cran
  - R
  - rstats
  - R package
  - purrr
  - "parallel computing"
  - multicore
  - "high-performanc computing"
actions:
  - label: GitHub Repository
    icon: github  
    url: "https://github.com/moosa-r/parapurrr"
  - label: Tutorial
    icon: manuals  
    url: "https://github.com/moosa-r/parapurrr#readme"
comments: false  # disable comments on this post
read_time: false # do not show read time
---

A simple yet fully customizable way to run functions iteratively in R using multiple CPU cores (instead of the default, one) by bridging purrr to foreach package and its adapters.

With parapurrr, one can run [purrr](https://cran.r-project.org/package=purrr "purrr: Functional Programming Tools")'s mapping functions in parallel (i.e., incorporate multiple CPU cores instead of the default, one). The package parapurrr does that by connecting [purrr](https://cran.r-project.org/package=purrr "purrr: Functional Programming Tools") to [foreach](https://cran.r-project.org/package=foreach "foreach: Provides Foreach Looping Construct") package and its adapters. Users are only required to add a prefix "pa\_" before their desired purrr functions (e.g., pa_map instead of map). The rest will be handled internally. All map family functions and all foreach adapters on CRAN are supported.

Please see the [parapurrr's vignette article](https://github.com/moosa-r/parapurrr#readme) for additional information, including FAQs and detailed instructions on fully tailoring the parallelization process.

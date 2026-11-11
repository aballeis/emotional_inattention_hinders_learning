library(tidyverse)

dir.create("data/built", showWarnings = FALSE)
time_code <- c("Wish I spent more time" = 1, "Happy with the amount of time I spend" = 0, "Wish I spent less time" = -1)

read_csv("data/survey_data.csv", show_col_types = FALSE, col_types = cols(.default = "c")) %>%
  filter(status == "completed", chesscom_frequency != "Less than once a month") %>%
  mutate(time_wins = unname(time_code[time_spent_wins]),
         time_losses = unname(time_code[time_spent_losses]),
         shift = time_losses - time_wins,
         rating = suppressWarnings(as.numeric(chesscom_rating)),
         rating = if_else(rating >= 100, rating, NA_real_),
         high_rating = rating >= median(rating, na.rm = TRUE)) %>%
  write_csv("data/built/survey_sample.csv", na = "")

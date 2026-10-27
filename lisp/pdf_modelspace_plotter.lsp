;; Model-space PDF plotter using the command-line -PLOT flow.
;; Core Console does not expose the ActiveX application object (vlax-get-acad-object
;; returns nil), so plotting must not rely on vla-* calls.
(vl-load-com)

(defun GNI:P-WRITE-RESULT (stream status reason plotter media /)
  (if stream
    (progn
      (princ "Status\tReason\tPlotter\tMedia\n" stream)
      (princ status stream)
      (princ "\t" stream)
      (princ reason stream)
      (princ "\t" stream)
      (princ (if plotter plotter "") stream)
      (princ "\t" stream)
      (princ (if media media "") stream)
      (princ "\n" stream))))

(defun GNI:P-BASIC-PDF-VALID (path / stream firstLine valid)
  (setq valid nil)
  (setq stream (open path "r"))
  (if stream
    (progn
      (setq firstLine (read-line stream))
      (close stream)
      (if (and firstLine
               (>= (strlen firstLine) 5)
               (= (substr firstLine 1 5) "%PDF-"))
        (setq valid T))))
  valid)

;; AcPlotRotation: 0 = 0deg, 1 = 90deg, 2 = 180deg, 3 = 270deg.
(defun GNI:P-ORIENTATION (rotation / pair)
  (cond
    ((= rotation 1) '("P" "N"))
    ((= rotation 2) '("L" "Y"))
    ((= rotation 3) '("P" "Y"))
    (T '("L" "N"))))

(defun GNI:RUN-PDF-PLOTTER
  (pdfPath resultPath plotterCandidates mediaCandidates plotStyle rotation
   / resultStream plotter media pair orientation upside style response failureReason)

  (setq resultStream (open resultPath "w"))
  (setq failureReason nil)
  (setq plotter (if plotterCandidates (car plotterCandidates) ""))
  (setq media (if mediaCandidates (car mediaCandidates) ""))
  (setq style (if (= plotStyle "") "." plotStyle))
  (setq pair (GNI:P-ORIENTATION rotation))
  (setq orientation (car pair))
  (setq upside (cadr pair))

  (if (not resultStream)
    (princ "\nGNI_PDF_PUBLISHER_FAILED:RESULT_FILE_OPEN_FAILED")
    (progn
      (if (= plotter "")
        (setq failureReason "PLOTTER_NOT_CONFIGURED"))

      (if (not failureReason)
        (progn
          (setvar "FILEDIA" 0)
          (setvar "CMDDIA" 0)
          (setvar "BACKGROUNDPLOT" 0)
          (setq response
            (vl-catch-all-apply
              '(lambda ()
                (command "_.-PLOT"
                  "Y" "Model" plotter media
                  "M" orientation upside
                  "E" "F" "C"
                  "Y" style "Y" ""
                  pdfPath "N" "Y"))))
          (if (vl-catch-all-error-p response)
            (setq failureReason
              (strcat
                "PLOT_COMMAND_ERROR:"
                (vl-catch-all-error-message response))))))

      (if (and (not failureReason)
               (not (GNI:P-BASIC-PDF-VALID pdfPath)))
        (setq failureReason "PLOT_TO_FILE_FAILED"))

      (if failureReason
        (progn
          (GNI:P-WRITE-RESULT
            resultStream "FAILED" failureReason plotter media)
          (princ
            (strcat "\nGNI_PDF_PUBLISHER_FAILED:" failureReason)))
        (progn
          (GNI:P-WRITE-RESULT
            resultStream "PASS" "OK" plotter media)
          (princ "\nGNI_PDF_PUBLISHER_OK")))

      (close resultStream)
      (princ "\nGNI_PDF_PUBLISHER_EXIT_READY")))
  (princ))

(princ "\nGNI_PDF_MODELSPACE_PLOTTER_0_4C_5_LOADED")

{ ============================================================================
  HighScore Plus User Script: Export ICDD Search & Match Results for SDL Bridge
  ============================================================================
  Usage inside Malvern Panalytical HighScore Plus:
    1. Configure a Parameter Set for:
         - Background Determination
         - Peak Search
         - Search & Match (connected to your ICDD PDF-4+ / PDF-5+ database)
    2. Save this script in HighScore Plus Script IDE as a User Script Parameter Set
       (e.g., "Export_SDL_Phase_CSV").
    3. Combine steps in a Smart Batch / User Batch:
         [Determine Background] -> [Search Peaks] -> [Execute Search & Match] -> [Export_SDL_Phase_CSV]
    4. Enable Automatic Processing (Customize -> Program Settings -> Automatic Processing)
       so that whenever xrd_sdl_bridge opens a new .xrdml file, HighScore Plus
       executes the batch and writes the identified phases to highscore_results/*.csv.
  ============================================================================ }

program ExportSDLPhaseCSV;

var
  OutDir, SampleName, OutFile: string;
  SL: TStringList;
  i: Integer;
  Pat: IPattern;
  RefCode, CompoundName, Formula: string;
  ScoreStr, ScaleStr: string;

begin
  { Adjust output folder to match your linked test_data\highscore_results directory }
  OutDir := 'C:\HighScore_Work\highscore_results\';
  ForceDirectories(OutDir);

  if Document.ScanCount > 0 then
    SampleName := ChangeFileExt(ExtractFileName(Document.FileName), '')
  else
    SampleName := 'Unknown_Scan';

  if SampleName = '' then
    SampleName := 'Untitled_Scan';

  OutFile := OutDir + SampleName + '_phases.csv';

  SL := TStringList.Create;
  try
    SL.Add('Sample,Rank,RefCode,CompoundName,Formula,Score,ScaleFactor,Timestamp');

    { Export accepted / matched candidate patterns from ICDD Search & Match }
    for i := 0 to Document.PatternCount - 1 do
    begin
      Pat := Document.Patterns[i];
      if Pat.Accepted then
      begin
        RefCode := Pat.ReferenceCode;
        CompoundName := StringReplace(Pat.CompoundName, ',', ';', [rfReplaceAll]);
        Formula := StringReplace(Pat.ChemicalFormula, ',', ';', [rfReplaceAll]);
        ScoreStr := IntToStr(Pat.Score);
        ScaleStr := FormatFloat('0.000', Pat.ScaleFactor);

        SL.Add(
          SampleName + ',' +
          IntToStr(i + 1) + ',' +
          RefCode + ',' +
          '"' + CompoundName + '",' +
          '"' + Formula + '",' +
          ScoreStr + ',' +
          ScaleStr + ',' +
          FormatDateTime('yyyy-mm-dd"T"hh:nn:ss', Now)
        );
      end;
    end;

    SL.SaveToFile(OutFile);
  finally
    SL.Free;
  end;
end.

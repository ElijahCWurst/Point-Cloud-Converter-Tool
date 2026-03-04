const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');

function createWindow() {
    const win = new BrowserWindow({
        width: 800,
        height: 600,
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            nodeIntegration: false,
            contextIsolation: true
        }
    });
    win.loadFile('index.html');
}

app.whenReady().then(createWindow);

app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
});

// --- NEW HANDLER: Background Pre-cache ---
ipcMain.handle('start-precache', async (event, inputPath) => {
    // We launch the process and don't wait for the result.
    // The Python script handles file locking to prevent conflicts.
    const pythonExecutable = path.join(__dirname, 'venv', 'Scripts', 'python.exe');
    const scriptPath = path.join(__dirname, 'backend', 'generate_chm.py');

    console.log(`Starting background pre-cache for: ${inputPath}`);
    
    // Pass the flag --precache
    spawn(pythonExecutable, ['-u', scriptPath, inputPath, '--precache']);
    
    return { status: 'started' };
});

// --- HANDLER: Generate Histogram ---
ipcMain.handle('generate-histogram', async (event, inputPath) => {
    return new Promise((resolve, reject) => {
        const pythonExecutable = path.join(__dirname, 'venv', 'Scripts', 'python.exe');
        const scriptPath = path.join(__dirname, 'backend', 'generate_histogram.py');
        const tempDir = app.getPath('temp'); 
        const imgName = `hist_${Date.now()}.png`;
        const outputPath = path.join(tempDir, imgName);

        const scriptArgs = ['-u', scriptPath, inputPath, outputPath];
        const pythonProcess = spawn(pythonExecutable, scriptArgs);

        let dataString = '';
        pythonProcess.stdout.on('data', (data) => { dataString += data.toString(); });
        pythonProcess.stderr.on('data', (data) => { console.error(`Histogram Error: ${data}`); });

        pythonProcess.on('close', (code) => {
            if (code === 0) {
                try {
                    const lines = dataString.trim().split('\n');
                    const lastLine = lines[lines.length - 1];
                    const jsonResponse = JSON.parse(lastLine);
                    resolve(jsonResponse);
                } catch (e) { reject(`Failed to parse histogram response.`); }
            } else { reject(`Histogram process exited with code ${code}`); }
        });
    });
});

// --- HANDLER: Run Python (CHM / Cover) ---
ipcMain.handle('run-python', async (event, args) => {
    const { inputPath, mode, resolution, thresholds } = args; 
    const win = BrowserWindow.fromWebContents(event.sender);
    const dir = path.dirname(inputPath);
    const ext = path.extname(inputPath);
    const name = path.basename(inputPath, ext);
    
    let baseOutputName = '';
    if (mode === 'cover') {
        const bandCount = (thresholds && thresholds.length > 0) ? thresholds.length + 1 : 1;
        baseOutputName = `${name}_${mode}_${bandCount}bands_${resolution}m.tif`;
    } else {
        baseOutputName = `${name}_${mode}_${resolution}m.tif`;
    }

    const { canceled, filePath } = await dialog.showSaveDialog(win, {
        title: 'Save Raster Output',
        defaultPath: path.join(dir, baseOutputName),
        filters: [
            { name: 'GeoTIFF', extensions: ['tif', 'tiff'] },
            { name: 'All Files', extensions: ['*'] }
        ]
    });

    if (canceled) {
        return { status: 'cancelled' };
    }

    return new Promise((resolve, reject) => {
        const pythonExecutable = path.join(__dirname, 'venv', 'Scripts', 'python.exe');
        
        let scriptName = '';
        if (mode === 'height') scriptName = 'generate_chm.py';
        else if (mode === 'cover') scriptName = 'generate_cover.py';
        else return reject(`Unknown mode selected: ${mode}`);

        const scriptPath = path.join(__dirname, 'backend', scriptName);
        const finalOutputPath = filePath;

        console.log(`Processing: ${inputPath}`);
        console.log(`Saving to: ${finalOutputPath}`);

        const scriptArgs = [
            '-u', scriptPath, inputPath, finalOutputPath, resolution
        ];

        const thresholdsToSend = thresholds || [];
        scriptArgs.push(JSON.stringify(thresholdsToSend));

        const pythonProcess = spawn(pythonExecutable, scriptArgs);

        let finalJsonResult = null;
        let capturedError = '';

        pythonProcess.stdout.on('data', (data) => {
            const outputStr = data.toString();
            const lines = outputStr.split('\n');
            lines.forEach(line => {
                const trimmed = line.trim();
                if (!trimmed) return;
                try {
                    const json = JSON.parse(trimmed);
                    if (json.progress !== undefined) {
                        event.sender.send('progress-update', json);
                    } else if (json.status !== undefined) {
                        finalJsonResult = json;
                    }
                } catch (e) {}
            });
        });

        pythonProcess.stderr.on('data', (data) => {
            console.error(`Python Error: ${data}`);
            capturedError += data.toString();
        });

        pythonProcess.on('close', (code) => {
            if (code === 0) {
                if (finalJsonResult) {
                    finalJsonResult.file = finalOutputPath; 
                    resolve(finalJsonResult);
                } else {
                    reject(`Process finished but returned no valid JSON result.`);
                }
            } else {
                reject(`Process exited with code ${code}. Stderr: ${capturedError}`);
            }
        });
    });
});